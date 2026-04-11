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
    timestamp = models.DateTimeField(auto_now_add=True)
    analysis = models.ForeignKey('Analysis', on_delete=models.CASCADE, related_name='snapshots')

    def populate(self, data: pd.DataFrame) -> None:
        """Parses a DataFrame of station data and creates Station, Fuel and Price objects."""
        with transaction.atomic():
            for _, row in data.iterrows():
                station, _ = Station.objects.update_or_create(
                    name=row.get('Name', ''),
                    defaults={
                        'city': row.get('city', ''),
                        'region': row.get('Region', ''),
                        'adress': row.get('Address', ''),
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

    @classmethod
    def get_active(cls):
        """Returns the single active analysis, or None."""
        return cls.objects.filter(active=True).first()

    def save(self, *args, **kwargs):
        """Ensure only one analysis is active at a time."""
        if self.active:
            Analysis.objects.exclude(pk=self.pk).filter(active=True).update(active=False)
        super().save(*args, **kwargs)


    def run_analysis(self):
        # Starts _run_analysis in a daemon thread so it doesn't block the main process.
        thread = threading.Thread(target=self._run_analysis, daemon=True, name=f"analysis-{self.pk}")
        thread.start()
        return thread

    def _run_analysis(self):
        while self.active:
            data = self._fetch_data()
            snapshot = Snapshot.objects.create(analysis=self)
            snapshot.populate(data)
            time.sleep(self.update_frequency * 60)

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