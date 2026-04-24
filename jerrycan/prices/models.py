import gzip
import json
import logging
import os
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

import pandas as pd
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, models, transaction
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

    timestamp = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DOWNLOADING,
    )
    price_count = models.PositiveIntegerField(default=0)
    analysis = models.ForeignKey('Analysis', on_delete=models.CASCADE, related_name='snapshots')

    def populate(self, data: pd.DataFrame) -> None:
        """Parses a DataFrame of station data and creates Station, Fuel and Price objects."""
        count = 0
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
                    count += 1
            self.price_count = count
            self.save(update_fields=['price_count'])

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

    @staticmethod
    def _has_running_import_task():
        transfer_task_model = globals().get('AnalysisTransferTask')
        if transfer_task_model is None:
            return False
        return transfer_task_model.objects.filter(
            task_type=transfer_task_model.TaskType.IMPORT,
            status__in=[
                transfer_task_model.Status.PENDING,
                transfer_task_model.Status.RUNNING,
            ],
        ).exists()

    def save(self, *args, **kwargs):
        """Ensure only one analysis is active at a time.
        Start the background thread when run_automatically is turned on.
        """
        # Do not switch current analysis while an import is still running.
        if self.active and self._has_running_import_task():
            self.active = False

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
        logger.info("Analysis #%s: background thread started", self.pk)
        while True:
            # Re-read from DB on every iteration to pick up field changes.
            try:
                analysis = Analysis.objects.get(pk=self.pk)
            except Analysis.DoesNotExist:
                logger.info("Analysis #%s: no longer exists, stopping thread", self.pk)
                break
            if not analysis.run_automatically:
                logger.info("Analysis #%s: run_automatically=False, stopping thread", self.pk)
                break

            snapshot = Snapshot.objects.create(
                analysis=analysis,
                status=Snapshot.Status.DOWNLOADING,
            )
            logger.info("Analysis #%s: snapshot #%s created, fetching data", self.pk, snapshot.pk)
            try:
                data = analysis._fetch_data()
            except Exception:
                logger.exception("Analysis #%s: snapshot #%s fetch failed", self.pk, snapshot.pk)
                snapshot.status = Snapshot.Status.ERROR
                snapshot.save(update_fields=['status'])
                time.sleep(analysis.update_frequency * 60)
                continue

            snapshot.status = Snapshot.Status.PROCESSING
            snapshot.save(update_fields=['status'])
            logger.info("Analysis #%s: snapshot #%s populating %d records", self.pk, snapshot.pk, len(data))
            try:
                snapshot.populate(data)
            except Exception:
                logger.exception("Analysis #%s: snapshot #%s populate failed", self.pk, snapshot.pk)
                snapshot.status = Snapshot.Status.ERROR
                snapshot.save(update_fields=['status'])
                time.sleep(analysis.update_frequency * 60)
                continue

            snapshot.status = Snapshot.Status.PROCESSED
            snapshot.price_count = Price.objects.filter(snapshot=snapshot).count()
            snapshot.save(update_fields=['status', 'price_count'])
            logger.info("Analysis #%s: snapshot #%s completed, %d prices recorded", self.pk, snapshot.pk, snapshot.price_count)
            time.sleep(analysis.update_frequency * 60)

    def _fetch_data(self):
        """Fetches the GeoJSON.gz from data_source_url and returns a flat pandas DataFrame.

        Each row represents one station with its coordinates and fuel prices as columns.
        """
        logger.debug("Analysis #%s: GET %s", self.pk, self.data_source_url)
        t0 = time.time()
        headers = {"User-Agent": "JerryCan/1.0 (fuel price tracker)"}
        response = requests.get(self.data_source_url, timeout=30, headers=headers)
        response.raise_for_status()
        logger.debug("Analysis #%s: HTTP %s received in %.1fs (%d bytes)", self.pk, response.status_code, time.time() - t0, len(response.content))

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

        logger.debug("Analysis #%s: parsed %d station records from GeoJSON", self.pk, len(records))
        return pd.DataFrame(records)


def _get_export_dir():
    """Return the directory for storing export/import files, creating it if needed."""
    export_dir = getattr(settings, 'ANALYSIS_EXPORT_DIR', None)
    if not export_dir:
        export_dir = os.path.join(settings.BASE_DIR, 'exports')
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


class TaskCancelledError(Exception):
    """Raised when a transfer task has been asked to stop."""


class AnalysisTransferTask(models.Model):
    class TaskType(models.TextChoices):
        EXPORT = 'export', 'Export'
        IMPORT = 'import', 'Import'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        ERROR = 'error', 'Error'

    task_type = models.CharField(max_length=6, choices=TaskType.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    analysis = models.ForeignKey(
        Analysis, on_delete=models.CASCADE, related_name='transfer_tasks',
        null=True, blank=True,
    )
    file_path = models.CharField(max_length=512, blank=True, default='')
    cancel_requested = models.BooleanField(default=False)
    status_detail = models.CharField(max_length=255, blank=True, default='')
    progress_percent = models.PositiveSmallIntegerField(default=0)
    activity_log = models.TextField(blank=True, default='')
    error_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def _log(self, message, *, detail=None, progress=None, save=True):
        timestamp = timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{timestamp}] {message}"
        if self.activity_log:
            self.activity_log = f"{self.activity_log}\n{line}"
        else:
            self.activity_log = line

        logger.info("[Task #%s] %s", self.pk, message)

        update_fields = ['activity_log']
        if detail is not None:
            self.status_detail = detail
            update_fields.append('status_detail')
        if progress is not None:
            self.progress_percent = max(0, min(100, int(progress)))
            update_fields.append('progress_percent')
        if save:
            self.save(update_fields=update_fields)

    def request_cancel(self):
        self.cancel_requested = True
        self.save(update_fields=['cancel_requested'])
        self._log('Stop requested by user', detail='Stopping task')

    def _check_cancel_requested(self):
        self.refresh_from_db(fields=['cancel_requested'])
        if self.cancel_requested:
            raise TaskCancelledError('Task cancelled by user')

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
            self.status_detail = 'Preparing export'
            self.progress_percent = 0
            self.save(update_fields=['status', 'status_detail', 'progress_percent'])
            self._log('Export started', detail='Preparing export', progress=0)
            self._do_export()
            self.status = self.Status.COMPLETED
            self.status_detail = 'Export completed'
            self.progress_percent = 100
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'progress_percent', 'completed_at'])
            self._log('Export completed', detail='Export completed', progress=100)
        except TaskCancelledError:
            self.status = self.Status.CANCELLED
            self.status_detail = 'Export cancelled'
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'completed_at'])
            self._log('Export cancelled by user', detail='Export cancelled')
        except Exception as exc:
            self.status = self.Status.ERROR
            self.error_message = str(exc)[:2000]
            self.status_detail = 'Export failed'
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'error_message', 'completed_at'])
            self._log(f"Export failed: {self.error_message}", detail='Export failed')

    def _do_export(self):
        self._check_cancel_requested()
        analysis = self.analysis
        if analysis is None:
            raise ValueError("No analysis associated with this export task")
        self._log('Collecting data for export', detail='Collecting data', progress=15)
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

            # Stations – load with pandas for speed
            f.write(',"stations":[')
            stations_df = pd.read_sql(
                """
                SELECT id, name, city, region, adress, longitude, latitude
                FROM prices_station
                WHERE analysis_id = %s
                ORDER BY id
                """,
                connection,
                params=[analysis.pk],
            )
            
            for idx, row in stations_df.iterrows():
                self._check_cancel_requested()
                if idx > 0:
                    f.write(',')
                station_record = {
                    'id': int(row['id']),
                    'name': row['name'],
                    'city': row['city'],
                    'region': row['region'],
                    'adress': row['adress'],
                    'longitude': float(row['longitude']),
                    'latitude': float(row['latitude']),
                }
                json.dump(station_record, f)
            f.write(']')

            # Snapshots – load with pandas for speed
            f.write(',"snapshots":[')
            snapshots_df = pd.read_sql(
                """
                SELECT id, timestamp, status
                FROM prices_snapshot
                WHERE analysis_id = %s
                ORDER BY id
                """,
                connection,
                params=[analysis.pk],
            )
            
            for idx, row in snapshots_df.iterrows():
                self._check_cancel_requested()
                if idx > 0:
                    f.write(',')
                snapshot_record = {
                    'id': int(row['id']),
                    'timestamp': row['timestamp'].isoformat() if hasattr(row['timestamp'], 'isoformat') else str(row['timestamp']),
                    'status': row['status'],
                }
                json.dump(snapshot_record, f)
            f.write(']')

            # Prices – load in bulk with pandas (much faster, avoids DB locks)
            f.write(',"prices":[')
            self._log('Loading prices from database', detail='Loading prices', progress=50)
            
            # Load all prices at once with pandas (faster than iterating)
            prices_df = pd.read_sql(
                """
                SELECT station_id, fuel_id, CAST(price AS FLOAT) as price, snapshot_id
                FROM prices_price
                WHERE snapshot_id IN (
                    SELECT id FROM prices_snapshot WHERE analysis_id = %s
                )
                ORDER BY id
                """,
                connection,
                params=[analysis.pk],
            )
            
            total_prices = len(prices_df)
            if total_prices:
                self._log(f'Exporting {total_prices:,} prices', detail='Exporting prices', progress=52)
            
            # Write prices in batches from pandas DataFrame
            next_progress_milestone = 5
            for idx, row in prices_df.iterrows():
                self._check_cancel_requested()
                
                if idx > 0:
                    f.write(',')
                
                price_record = {
                    'station_id': int(row['station_id']),
                    'fuel_id': int(row['fuel_id']),
                    'price': float(row['price']),
                    'snapshot_id': int(row['snapshot_id']),
                }
                json.dump(price_record, f)
                
                # Update progress every 10000 records to reduce DB saves
                if total_prices and (idx + 1) % 10000 == 0:
                    ratio = (idx + 1) / total_prices
                    progress = 52 + int(ratio * 38)
                    self.status_detail = f'Exporting prices ({idx + 1:,}/{total_prices:,})'
                    self.progress_percent = max(52, min(90, progress))
                    self.save(update_fields=['status_detail', 'progress_percent'])
                    
                    current_pct = int(ratio * 100)
                    while current_pct >= next_progress_milestone and next_progress_milestone <= 100:
                        msg = f'Prices export progress: {next_progress_milestone}% ({idx + 1:,}/{total_prices:,})'
                        self._log(msg, detail=self.status_detail, progress=self.progress_percent)

            if total_prices > 0:
                self._log(
                    f'Exported {total_prices:,} prices',
                    detail=f'Exporting prices ({total_prices:,}/{total_prices:,})',
                    progress=90,
                )
            
            f.write(']')

            f.write('}')

        self.file_path = filepath
        self.status_detail = 'Writing export file complete'
        self.progress_percent = 95
        self.save(update_fields=['file_path', 'status_detail', 'progress_percent'])
        self._log('Export file generated', detail='Finalizing export', progress=95)

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
            self.status_detail = 'Preparing import'
            self.progress_percent = 0
            self.save(update_fields=['status', 'status_detail', 'progress_percent'])
            self._log('Import started', detail='Preparing import', progress=0)
            should_activate = self._do_import()
            self.status = self.Status.COMPLETED
            self.status_detail = 'Import completed'
            self.progress_percent = 100
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'progress_percent', 'completed_at'])

            # Activate imported analysis only after import task is completed.
            if should_activate and self.analysis_id:
                analysis = self.analysis
                analysis.active = True
                analysis.save(update_fields=['active'])

            self._log('Import completed', detail='Import completed', progress=100)
        except TaskCancelledError:
            self.status = self.Status.CANCELLED
            self.status_detail = 'Import cancelled'
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'completed_at'])
            self._log('Import cancelled by user', detail='Import cancelled')
        except Exception as exc:
            self.status = self.Status.ERROR
            self.error_message = str(exc)[:2000]
            self.status_detail = 'Import failed'
            self.completed_at = timezone.now()
            self.save(update_fields=['status', 'status_detail', 'error_message', 'completed_at'])
            self._log(f"Import failed: {self.error_message}", detail='Import failed')

    def _do_import(self):
        self._check_cancel_requested()
        filepath = self.file_path
        self._log('Reading archive', detail='Reading archive', progress=5)
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            data = json.load(f)
        self._check_cancel_requested()
        self._log('Archive loaded', detail='Validating archive', progress=8)

        version = data.get('version', 1)
        if version != 1:
            raise ValueError(f"Unsupported export version: {version}")

        analysis_data = data['analysis']
        should_activate = bool(analysis_data.get('active', False))
        # Don't auto-run on import
        analysis_data['run_automatically'] = False
        self._log('Creating analysis record', detail='Creating analysis', progress=10)

        with transaction.atomic():
            analysis = Analysis(
                update_frequency=analysis_data.get('update_frequency', 5),
                data_source_url=analysis_data.get('data_source_url', ''),
                active=False,
                run_automatically=False,
                max_snapshots=analysis_data.get('max_snapshots', 100),
            )
            # Use super().save() to avoid the custom save logic
            models.Model.save(analysis)

        self.analysis = analysis
        self.save(update_fields=['analysis'])
        self._log(f'Analysis #{analysis.pk} created', detail='Importing fuels', progress=20)
        self._check_cancel_requested()

        # Fuels: get_or_create by name, build ID mapping (fast, only a handful of fuels)
        fuel_id_map = {}
        for fuel_data in data.get('fuels', []):
            self._check_cancel_requested()
            fuel, _ = Fuel.objects.get_or_create(name=fuel_data['name'])
            fuel_id_map[fuel_data['id']] = fuel.pk
        self._log(f'Imported {len(fuel_id_map)} fuel types', detail='Importing stations', progress=30)

        # Keep mapping inserts atomic, then insert prices in short batches to reduce
        # lock duration for concurrent read requests (e.g. polling task status).
        with transaction.atomic():
            # Stations: bulk_create with large batches
            station_id_map = {}
            station_data_list = data.get('stations', [])
            station_objs = [
                Station(
                    name=s['name'], city=s['city'], region=s['region'],
                    adress=s['adress'], longitude=s['longitude'],
                    latitude=s['latitude'], analysis=analysis,
                )
                for s in station_data_list
            ]
            idx = 0
            for i in range(0, len(station_objs), 10000):
                self._check_cancel_requested()
                created = Station.objects.bulk_create(station_objs[i:i + 10000])
                for obj in created:
                    station_id_map[station_data_list[idx]['id']] = obj.pk
                    idx += 1
                if station_data_list:
                    self._log(
                        f'Stations imported: {idx}/{len(station_data_list)}',
                        detail=f'Importing stations ({idx}/{len(station_data_list)})',
                        progress=30 + int((idx / len(station_data_list)) * 15),
                    )
            self._log(
                f'Imported {len(station_id_map)} stations',
                detail='Importing snapshots',
                progress=45,
            )

            # Snapshots: bulk_create with large batches
            snapshot_id_map = {}
            snapshot_data_list = data.get('snapshots', [])
            snapshot_objs = [
                Snapshot(
                    analysis=analysis,
                    status=snap.get('status', Snapshot.Status.PROCESSED),
                    timestamp=datetime.fromisoformat(snap['timestamp']) if isinstance(snap.get('timestamp'), str) else snap.get('timestamp'),
                )
                for snap in snapshot_data_list
            ]
            idx = 0
            for i in range(0, len(snapshot_objs), 10000):
                self._check_cancel_requested()
                created = Snapshot.objects.bulk_create(snapshot_objs[i:i + 10000])
                for obj in created:
                    snapshot_id_map[snapshot_data_list[idx]['id']] = obj.pk
                    idx += 1
                if snapshot_data_list:
                    self._log(
                        f'Snapshots imported: {idx}/{len(snapshot_data_list)}',
                        detail=f'Importing snapshots ({idx}/{len(snapshot_data_list)})',
                        progress=45 + int((idx / len(snapshot_data_list)) * 15),
                    )
            self._log(
                f'Imported {len(snapshot_id_map)} snapshots',
                detail='Importing prices',
                progress=60,
            )

        # Prices: raw SQL executemany in batches to avoid one long DB write lock.
        price_table = Price._meta.db_table
        price_sql = (
            f"INSERT INTO {price_table} (station_id, fuel_id, snapshot_id, price) "
            f"VALUES (%s, %s, %s, %s)"
        )

        batch = []
        batch_size = 5000
        inserted_prices = 0
        total_prices = len(data.get('prices', []))
        next_progress_milestone = 5
        if total_prices:
            self._log(f'Importing {total_prices:,} prices', detail='Importing prices', progress=60)
        with connection.cursor() as cursor:
            for pr in data.get('prices', []):
                ms = station_id_map.get(pr['station_id'])
                mf = fuel_id_map.get(pr['fuel_id'])
                mn = snapshot_id_map.get(pr['snapshot_id'])
                if ms is None or mf is None or mn is None:
                    continue
                batch.append((ms, mf, mn, pr['price']))
                if len(batch) >= batch_size:
                    self._check_cancel_requested()
                    with transaction.atomic():
                        cursor.executemany(price_sql, batch)
                    inserted_prices += len(batch)
                    if total_prices:
                        ratio = inserted_prices / total_prices
                        progress = 60 + int(ratio * 35)
                        self.status_detail = f'Importing prices ({inserted_prices:,}/{total_prices:,})'
                        self.progress_percent = max(60, min(95, progress))
                        self.save(update_fields=['status_detail', 'progress_percent'])

                        current_pct = int(ratio * 100)
                        while current_pct >= next_progress_milestone and next_progress_milestone <= 100:
                            msg = f'Prices import progress: {next_progress_milestone}% ({inserted_prices:,}/{total_prices:,})'
                            self._log(
                                msg,
                                detail=self.status_detail,
                                progress=self.progress_percent,
                            )
                            next_progress_milestone += 5
                    batch.clear()
            if batch:
                self._check_cancel_requested()
                with transaction.atomic():
                    cursor.executemany(price_sql, batch)
                inserted_prices += len(batch)
                if total_prices:
                    ratio = inserted_prices / total_prices
                    progress = 60 + int(ratio * 35)
                    detail = f'Importing prices ({inserted_prices:,}/{total_prices:,})'
                    bounded_progress = max(60, min(95, progress))
                    current_pct = int(ratio * 100)
                    while current_pct >= next_progress_milestone and next_progress_milestone <= 100:
                        msg = f'Prices import progress: {next_progress_milestone}% ({inserted_prices:,}/{total_prices:,})'
                        self._log(msg, detail=detail, progress=bounded_progress)
                        next_progress_milestone += 5
                    self._log(
                        f'Prices imported: {inserted_prices:,}/{total_prices:,}',
                        detail=detail,
                        progress=bounded_progress,
                    )

        self._log(
            f'Imported {inserted_prices:,} prices',
            detail='Finalizing import',
            progress=95,
        )

        # Update cached price counts on all snapshots for this analysis.
        self._log('Updating snapshot price counts', detail='Finalizing import', progress=96)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {Snapshot._meta.db_table} SET price_count = ("
                f"  SELECT COUNT(*) FROM {Price._meta.db_table}"
                f"  WHERE {Price._meta.db_table}.snapshot_id = {Snapshot._meta.db_table}.id"
                f") WHERE analysis_id = %s",
                [analysis.pk],
            )

        return should_activate


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