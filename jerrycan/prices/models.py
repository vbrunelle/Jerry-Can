import gzip
import json
import os
import threading
import time

import pandas as pd
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


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


def _get_export_dir():
    """Return the directory for storing export/import files, creating it if needed."""
    export_dir = getattr(settings, 'ANALYSIS_EXPORT_DIR', None)
    if not export_dir:
        export_dir = os.path.join(settings.BASE_DIR, 'exports')
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


class AnalysisTransferTask(models.Model):
    class TaskType(models.TextChoices):
        EXPORT = 'export', 'Export'
        IMPORT = 'import', 'Import'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        ERROR = 'error', 'Error'

    task_type = models.CharField(max_length=6, choices=TaskType.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    analysis = models.ForeignKey(
        Analysis, on_delete=models.CASCADE, related_name='transfer_tasks',
        null=True, blank=True,
    )
    file_path = models.CharField(max_length=512, blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # --- Export ---------------------------------------------------------------

    def start_export(self):
        """Launch export in a background thread."""
        thread = threading.Thread(target=self._run_export, daemon=True,
                                  name=f"export-{self.pk}")
        thread.start()
        return thread

    def _run_export(self):
        try:
            self.status = self.Status.RUNNING
            self.save(update_fields=['status'])
            self._do_export()
            self.status = self.Status.COMPLETED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at'])
        except Exception as exc:
            self.status = self.Status.ERROR
            self.error_message = str(exc)[:2000]
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'error_message', 'completed_at'])

    def _do_export(self):
        analysis = self.analysis
        if analysis is None:
            raise ValueError("No analysis associated with this export task")
        export_dir = _get_export_dir()
        filepath = os.path.join(export_dir, f'analysis_{analysis.pk}_{self.pk}.json.gz')

        with gzip.open(filepath, 'wt', encoding='utf-8') as f:
            f.write('{"version":1,')

            # Analysis metadata
            f.write('"analysis":')
            json.dump({
                'update_frequency': analysis.update_frequency,
                'data_source_url': analysis.data_source_url,
                'active': analysis.active,
                'run_automatically': analysis.run_automatically,
                'max_snapshots': analysis.max_snapshots,
            }, f)

            # Fuels used by this analysis
            fuel_ids = (Price.objects
                        .filter(snapshot__analysis=analysis)
                        .values_list('fuel_id', flat=True)
                        .distinct())
            fuels = list(Fuel.objects.filter(pk__in=fuel_ids).values('id', 'name'))
            f.write(',"fuels":')
            json.dump(fuels, f)

            # Stations
            f.write(',"stations":[')
            first = True
            for st in (Station.objects.filter(analysis=analysis)
                       .values('id', 'name', 'city', 'region', 'adress',
                               'longitude', 'latitude')
                       .iterator(chunk_size=2000)):
                if not first:
                    f.write(',')
                json.dump(st, f)
                first = False
            f.write(']')

            # Snapshots
            f.write(',"snapshots":[')
            first = True
            for snap in (Snapshot.objects.filter(analysis=analysis)
                         .values('id', 'timestamp', 'status')
                         .iterator(chunk_size=2000)):
                if not first:
                    f.write(',')
                snap['timestamp'] = snap['timestamp'].isoformat()
                json.dump(snap, f)
                first = False
            f.write(']')

            # Prices – streamed in chunks
            f.write(',"prices":[')
            first = True
            for pr in (Price.objects.filter(snapshot__analysis=analysis)
                       .values('station_id', 'fuel_id', 'price', 'snapshot_id')
                       .iterator(chunk_size=5000)):
                if not first:
                    f.write(',')
                pr['price'] = float(pr['price'])
                json.dump(pr, f)
                first = False
            f.write(']')

            f.write('}')

        self.file_path = filepath
        self.save(update_fields=['file_path'])

    # --- Import ---------------------------------------------------------------

    def start_import(self):
        """Launch import in a background thread."""
        thread = threading.Thread(target=self._run_import, daemon=True,
                                  name=f"import-{self.pk}")
        thread.start()
        return thread

    def _run_import(self):
        try:
            self.status = self.Status.RUNNING
            self.save(update_fields=['status'])
            self._do_import()
            self.status = self.Status.COMPLETED
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'completed_at'])
        except Exception as exc:
            self.status = self.Status.ERROR
            self.error_message = str(exc)[:2000]
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'error_message', 'completed_at'])

    def _do_import(self):
        filepath = self.file_path
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            data = json.load(f)

        version = data.get('version', 1)
        if version != 1:
            raise ValueError(f"Unsupported export version: {version}")

        analysis_data = data['analysis']
        # Don't auto-run on import
        analysis_data['run_automatically'] = False

        with transaction.atomic():
            analysis = Analysis(
                update_frequency=analysis_data.get('update_frequency', 5),
                data_source_url=analysis_data.get('data_source_url', ''),
                active=analysis_data.get('active', False),
                run_automatically=False,
                max_snapshots=analysis_data.get('max_snapshots', 100),
            )
            # Use super().save() to avoid the custom save logic
            models.Model.save(analysis)

        self.analysis = analysis
        self.save(update_fields=['analysis'])

        # Fuels: get_or_create by name, build ID mapping
        fuel_id_map = {}
        for fuel_data in data.get('fuels', []):
            fuel, _ = Fuel.objects.get_or_create(name=fuel_data['name'])
            fuel_id_map[fuel_data['id']] = fuel.pk

        # Stations: bulk_create, build ID mapping
        station_id_map = {}
        station_objs = []
        for s in data.get('stations', []):
            station_objs.append(Station(
                name=s['name'],
                city=s['city'],
                region=s['region'],
                adress=s['adress'],
                longitude=s['longitude'],
                latitude=s['latitude'],
                analysis=analysis,
            ))
        BATCH = 2000
        idx = 0
        station_data_list = data.get('stations', [])
        for i in range(0, len(station_objs), BATCH):
            created = Station.objects.bulk_create(station_objs[i:i + BATCH])
            for j, obj in enumerate(created):
                old_id = station_data_list[idx]['id']
                station_id_map[old_id] = obj.pk
                idx += 1

        # Snapshots: bulk_create, then fix timestamps
        snapshot_id_map = {}
        snapshot_objs = []
        snapshot_data_list = data.get('snapshots', [])
        for snap in data.get('snapshots', []):
            snapshot_objs.append(Snapshot(
                analysis=analysis,
                status=snap.get('status', Snapshot.Status.PROCESSED),
            ))

        created_snapshots = []
        idx = 0
        for i in range(0, len(snapshot_objs), BATCH):
            created = Snapshot.objects.bulk_create(snapshot_objs[i:i + BATCH])
            created_snapshots.extend(created)
            for j, obj in enumerate(created):
                old_id = snapshot_data_list[idx]['id']
                snapshot_id_map[old_id] = obj.pk
                idx += 1

        # Fix snapshot timestamps using model-level bulk_update.
        # This preserves original snapshot capture time instead of import time.
        for obj, snap in zip(created_snapshots, snapshot_data_list):
            ts_str = snap['timestamp']
            ts_val = timezone.datetime.fromisoformat(ts_str)
            if timezone.is_naive(ts_val):
                ts_val = timezone.make_aware(ts_val)
            obj.timestamp = ts_val

        if created_snapshots:
            for i in range(0, len(created_snapshots), BATCH):
                Snapshot.objects.bulk_update(
                    created_snapshots[i:i + BATCH],
                    ['timestamp'],
                    batch_size=BATCH,
                )

        # Prices: bulk_create in batches
        price_batch = []
        for pr in data.get('prices', []):
            mapped_station = station_id_map.get(pr['station_id'])
            mapped_fuel = fuel_id_map.get(pr['fuel_id'])
            mapped_snapshot = snapshot_id_map.get(pr['snapshot_id'])
            if mapped_station is None or mapped_fuel is None or mapped_snapshot is None:
                continue
            price_batch.append(Price(
                station_id=mapped_station,
                fuel_id=mapped_fuel,
                snapshot_id=mapped_snapshot,
                price=pr['price'],
            ))
            if len(price_batch) >= BATCH:
                Price.objects.bulk_create(price_batch)
                price_batch = []
        if price_batch:
            Price.objects.bulk_create(price_batch)


class ChunkedUpload(models.Model):
    """Track chunked file uploads in progress."""
    class Status(models.TextChoices):
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        ERROR = 'error', 'Error'

    upload_id = models.CharField(max_length=64, unique=True, db_index=True)
    filename = models.CharField(max_length=256)
    total_chunks = models.PositiveIntegerField()
    received_chunks = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.IN_PROGRESS)
    temp_dir = models.CharField(max_length=512)
    final_path = models.CharField(max_length=512, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def get_chunk_path(self, chunk_number):
        """Return the path for a specific chunk file."""
        return os.path.join(self.temp_dir, f'chunk_{chunk_number}')

    def all_chunks_received(self):
        """Check if all chunks have been received."""
        return self.received_chunks >= self.total_chunks

    def assemble_file(self):
        """Assemble all chunks into the final file."""
        export_dir = _get_export_dir()
        final_path = os.path.join(export_dir, f'import_{self.upload_id}.json.gz')

        with open(final_path, 'wb') as out_file:
            for i in range(self.total_chunks):
                chunk_path = self.get_chunk_path(i)
                if not os.path.exists(chunk_path):
                    raise FileNotFoundError(f"Chunk {i} missing at {chunk_path}")
                with open(chunk_path, 'rb') as chunk_file:
                    out_file.write(chunk_file.read())

        self.final_path = final_path
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=['final_path', 'status', 'completed_at'])

        # Clean up temp directory
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

        return final_path

    def cleanup(self):
        """Remove temporary files."""
        try:
            import shutil
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception:
            pass