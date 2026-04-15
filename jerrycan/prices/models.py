import gzip
import json
import threading
import time

import pandas as pd
import requests
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    must_change_password = models.BooleanField(default=False)
    # Stored in plain text only while must_change_password is True; cleared on password change.
    temporary_password = models.CharField(max_length=64, blank=True, default='')


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


class Snapshot(models.Model):
    class Status(models.TextChoices):
        DOWNLOADING = 'downloading', 'Downloading'
        PROCESSING  = 'processing',  'Processing'
        PROCESSED   = 'processed',   'Processed'
        ERROR       = 'error',       'Error'

    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DOWNLOADING,
    )
    analysis = models.ForeignKey('Analysis', on_delete=models.CASCADE, related_name='snapshots')

    def populate(self, data: pd.DataFrame) -> None:
        """Parses a DataFrame of station data and creates Station, Fuel and Price objects."""
        with transaction.atomic():
            for _, row in data.iterrows():
                raw_address = row.get('Address', '')
                address_parts = raw_address.rsplit(',', 1)
                city = address_parts[1].strip() if len(address_parts) > 1 else ''
                street = address_parts[0].strip()
                station, _ = Station.objects.update_or_create(
                    name=row.get('Name', ''),
                    defaults={
                        'city': city,
                        'region': row.get('Region', ''),
                        'adress': street,
                        'longitude': row.get('longitude', 0.0),
                        'latitude': row.get('latitude', 0.0),
                        'analysis': self.analysis,
                    }
                )

                for entry in row.get('Prices') or []:
                    if not isinstance(entry, dict) or not entry.get('IsAvailable'):
                        continue
                    raw_price = entry.get('Price') or ''
                    try:
                        value = float(raw_price.replace('\xa0', '').replace('¢', '').strip())
                    except (ValueError, AttributeError):
                        continue
                    fuel_name = entry.get('GasType', '').strip()
                    if not fuel_name:
                        continue
                    fuel, _ = Fuel.objects.get_or_create(name=fuel_name)
                    Price.objects.create(
                        station=station,
                        fuel=fuel,
                        snapshot=self,
                        price=value,
                    )

class Station(models.Model):
    name = models.CharField(max_length=255)
    city = models.CharField(max_length=255)
    region = models.CharField(max_length=255)
    adress = models.CharField(max_length=255)
    longitude = models.FloatField()
    latitude = models.FloatField()
    analysis = models.ForeignKey('Analysis', on_delete=models.CASCADE, related_name='stations')

class Fuel(models.Model):
    # Typically this would be something like "Regular", "Premium", "Diesel", etc.
    name = models.CharField(max_length=255)

class Price(models.Model):
    station = models.ForeignKey(Station, on_delete=models.CASCADE)
    fuel = models.ForeignKey(Fuel, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    snapshot = models.ForeignKey(Snapshot, on_delete=models.CASCADE, related_name='prices')


class Analysis(models.Model):
    update_frequency = models.IntegerField(help_text="Update frequency in minutes", default=5)
    data_source_url = models.URLField(help_text="URL of the data source to collect fuel prices from", default="https://regieessencequebec.ca/stations.geojson.gz")
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    run_automatically = models.BooleanField(
        default=False,
        help_text="Automatically take snapshots at the configured frequency.",
    )
    max_snapshots = models.PositiveIntegerField(
        default=100,
        help_text="Maximum number of snapshots to display in pivot tables.",
    )

    @classmethod
    def get_active(cls):
        """Returns the single active analysis, or None."""
        return cls.objects.filter(active=True).first()

    def save(self, *args, **kwargs):
        """Ensure only one analysis is active at a time.
        Start the background thread when run_automatically is turned on.
        """
        if self.active:
            Analysis.objects.exclude(pk=self.pk).filter(active=True).update(active=False)

        # Start thread when run_automatically is True and wasn't before.
        # Covers two cases:
        #   - New object created with run_automatically=True (self.pk is None)
        #   - Existing object updated from False → True
        start_thread = False
        if self.pk:
            try:
                previous = Analysis.objects.get(pk=self.pk)
                if not previous.run_automatically and self.run_automatically:
                    start_thread = True
            except Analysis.DoesNotExist:
                pass
        elif self.run_automatically:
            # Brand-new object with run_automatically=True from the start.
            start_thread = True

        super().save(*args, **kwargs)

        if start_thread:
            self.run_analysis()


    def run_analysis(self):
        # Starts _run_analysis in a daemon thread so it doesn't block the main process.
        thread = threading.Thread(target=self._run_analysis, daemon=True, name=f"analysis-{self.pk}")
        thread.start()
        return thread

    def _run_analysis(self):
        while True:
            # Re-read from DB on every iteration to pick up field changes.
            try:
                analysis = Analysis.objects.get(pk=self.pk)
            except Analysis.DoesNotExist:
                break
            if not analysis.run_automatically:
                break

            snapshot = Snapshot.objects.create(
                analysis=analysis,
                status=Snapshot.Status.DOWNLOADING,
            )
            try:
                data = analysis._fetch_data()
            except Exception:
                snapshot.status = Snapshot.Status.ERROR
                snapshot.save(update_fields=['status'])
                time.sleep(analysis.update_frequency * 60)
                continue

            snapshot.status = Snapshot.Status.PROCESSING
            snapshot.save(update_fields=['status'])
            try:
                snapshot.populate(data)
            except Exception:
                snapshot.status = Snapshot.Status.ERROR
                snapshot.save(update_fields=['status'])
                time.sleep(analysis.update_frequency * 60)
                continue

            snapshot.status = Snapshot.Status.PROCESSED
            snapshot.save(update_fields=['status'])
            time.sleep(analysis.update_frequency * 60)

    def _fetch_data(self):
        """Fetches the GeoJSON.gz from data_source_url and returns a flat pandas DataFrame.

        Each row represents one station with its coordinates and fuel prices as columns.
        """
        headers = {"User-Agent": "JerryCan/1.0 (fuel price tracker)"}
        response = requests.get(self.data_source_url, timeout=30, headers=headers)
        response.raise_for_status()

        try:
            raw = gzip.decompress(response.content)
        except gzip.BadGzipFile:
            raw = response.content
        geojson = json.loads(raw)

        records = []
        for feature in geojson.get('features', []):
            props = feature.get('properties', {}) or {}
            coords = (feature.get('geometry') or {}).get('coordinates', [None, None])
            record = {**props, 'longitude': coords[0], 'latitude': coords[1]}
            records.append(record)

        return pd.DataFrame(records)