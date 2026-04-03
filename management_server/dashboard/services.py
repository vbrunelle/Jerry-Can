"""Services for data inspection and CSV generation."""

import csv
import os
import sqlite3
import uuid
from datetime import timedelta

from django.utils import timezone


DATABASE_PATH = os.getenv('DATABASE_PATH', '/data/fuel_prices.db')
PERSISTENCE_BACKEND = os.getenv('PERSISTENCE_BACKEND', 'sqlite')


def _connect(db_path):
    """Return a sqlite3 connection with Row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _empty_inspection_data():
    """Return empty inspection data when no database is available."""
    return {
        'summary': {
            'station_count': 0,
            'price_count': 0,
            'snapshot_count': 0,
            'first_snapshot': None,
            'last_snapshot': None,
        },
        'regions': [],
        'latest_prices': [],
        'snapshots': [],
        'price_variations': {'increases': [], 'decreases': []},
    }


def get_inspection_data():
    """Return a dict with inspection data from the SQLite database."""
    db_path = DATABASE_PATH
    if not os.path.exists(db_path):
        return _empty_inspection_data()

    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return _empty_inspection_data()

    try:
        # --- Summary ---
        station_count = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        price_count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        snapshot_count = conn.execute(
            "SELECT COUNT(DISTINCT fetched_at) FROM prices"
        ).fetchone()[0]

        first_snapshot = None
        last_snapshot = None
        if price_count:
            row = conn.execute(
                "SELECT MIN(fetched_at), MAX(fetched_at) FROM prices"
            ).fetchone()
            first_snapshot = row[0]
            last_snapshot = row[1]

        summary = {
            'station_count': station_count,
            'price_count': price_count,
            'snapshot_count': snapshot_count,
            'first_snapshot': first_snapshot,
            'last_snapshot': last_snapshot,
        }

        # --- Regions ---
        count_rows = conn.execute(
            """
            SELECT region, COUNT(*) as station_count
            FROM stations
            GROUP BY region
            ORDER BY station_count DESC
            """
        ).fetchall()

        avg_rows = conn.execute(
            """
            SELECT s.region, p.fuel_type,
                   ROUND(AVG(p.price), 1) AS avg_price
            FROM prices p
            JOIN stations s ON s.id = p.station_id
            JOIN (
                SELECT station_id, fuel_type, MAX(fetched_at) AS max_ts
                FROM prices
                GROUP BY station_id, fuel_type
            ) latest
              ON p.station_id = latest.station_id
             AND p.fuel_type  = latest.fuel_type
             AND p.fetched_at = latest.max_ts
            GROUP BY s.region, p.fuel_type
            ORDER BY s.region
            """
        ).fetchall()

        avg_map = {}
        for row in avg_rows:
            region_key = row['region'] or 'N/A'
            avg_map.setdefault(region_key, {})[row['fuel_type']] = row['avg_price']

        regions = []
        for r in count_rows:
            region_name = r['region'] or 'N/A'
            entry = {
                'region': region_name,
                'station_count': r['station_count'],
                'avg_prices': avg_map.get(region_name, {}),
            }
            regions.append(entry)

        # --- Latest prices (lowest 50) ---
        latest_ts = conn.execute("SELECT MAX(fetched_at) FROM prices").fetchone()[0]
        latest_prices = []
        if latest_ts:
            rows = conn.execute(
                """
                SELECT s.name, s.city, s.region, p.fuel_type, p.price
                FROM prices p
                JOIN stations s ON s.id = p.station_id
                WHERE p.fetched_at = ?
                ORDER BY p.price ASC
                LIMIT 50
                """,
                (latest_ts,),
            ).fetchall()
            latest_prices = [
                {
                    'station_name': row['name'],
                    'city': row['city'],
                    'region': row['region'],
                    'fuel_type': row['fuel_type'],
                    'price': row['price'],
                }
                for row in rows
            ]

        # --- Snapshots (last 10) ---
        snap_rows = conn.execute(
            """
            SELECT fetched_at, COUNT(*) as record_count
            FROM prices
            GROUP BY fetched_at
            ORDER BY fetched_at DESC
            LIMIT 10
            """
        ).fetchall()
        snapshots = [
            {'fetched_at': row['fetched_at'], 'record_count': row['record_count']}
            for row in snap_rows
        ]

        # --- Price variations ---
        _CTE = """
            WITH all_pairs AS (
                SELECT
                    p.station_id,
                    s.name  AS station_name,
                    s.city,
                    p.fuel_type,
                    p.fetched_at,
                    p.price,
                    LAG(p.price) OVER (
                        PARTITION BY p.station_id, p.fuel_type
                        ORDER BY p.fetched_at
                    ) AS prev_price,
                    LAG(p.fetched_at) OVER (
                        PARTITION BY p.station_id, p.fuel_type
                        ORDER BY p.fetched_at
                    ) AS prev_fetched_at
                FROM prices p
                JOIN stations s ON s.id = p.station_id
            ),
            last_changes AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY station_id, fuel_type
                        ORDER BY fetched_at DESC
                    ) AS rn
                FROM all_pairs
                WHERE prev_price IS NOT NULL AND price != prev_price
            ),
            final AS (
                SELECT
                    station_name, city, fuel_type,
                    prev_fetched_at, fetched_at,
                    ROUND(prev_price, 1) AS prev_price,
                    ROUND(price, 1)      AS price,
                    ROUND(price - prev_price, 1) AS delta
                FROM last_changes
                WHERE rn = 1
            )
        """
        increases = []
        decreases = []
        try:
            rise_rows = conn.execute(
                _CTE + "SELECT * FROM final WHERE delta > 0 ORDER BY delta DESC LIMIT 10"
            ).fetchall()
            increases = [
                {
                    'station_name': r['station_name'],
                    'city': r['city'],
                    'fuel_type': r['fuel_type'],
                    'prev_price': r['prev_price'],
                    'price': r['price'],
                    'delta': r['delta'],
                }
                for r in rise_rows
            ]

            drop_rows = conn.execute(
                _CTE + "SELECT * FROM final WHERE delta < 0 ORDER BY delta ASC LIMIT 10"
            ).fetchall()
            decreases = [
                {
                    'station_name': r['station_name'],
                    'city': r['city'],
                    'fuel_type': r['fuel_type'],
                    'prev_price': r['prev_price'],
                    'price': r['price'],
                    'delta': r['delta'],
                }
                for r in drop_rows
            ]
        except sqlite3.OperationalError:
            pass

        return {
            'summary': summary,
            'regions': regions,
            'latest_prices': latest_prices,
            'snapshots': snapshots,
            'price_variations': {'increases': increases, 'decreases': decreases},
        }
    except sqlite3.Error:
        return _empty_inspection_data()
    finally:
        conn.close()


def generate_csv_for_user(download_request_id):
    """Generate a CSV file for the given DownloadRequest."""
    from dashboard.models import DownloadRequest

    try:
        dr = DownloadRequest.objects.get(pk=download_request_id)
    except DownloadRequest.DoesNotExist:
        return

    dr.status = 'processing'
    dr.save(update_fields=['status'])

    try:
        db_path = DATABASE_PATH
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database not found: {db_path}")

        conn = _connect(db_path)
        try:
            rows = conn.execute(
                """
                SELECT s.name AS station_name, s.address, s.city, s.region,
                       s.latitude, s.longitude,
                       p.fuel_type, p.price, p.fetched_at
                FROM prices p
                JOIN stations s ON s.id = p.station_id
                ORDER BY p.fetched_at DESC, s.region, s.name, p.fuel_type
                """
            ).fetchall()
        finally:
            conn.close()

        download_dir = f"/data/downloads/{dr.user_id}"
        os.makedirs(download_dir, exist_ok=True)

        filename = f"{uuid.uuid4()}.csv"
        file_path = os.path.join(download_dir, filename)

        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'station_name', 'address', 'city', 'region',
                'latitude', 'longitude', 'fuel_type', 'price', 'fetched_at',
            ])
            for row in rows:
                writer.writerow([
                    row['station_name'], row['address'], row['city'],
                    row['region'], row['latitude'], row['longitude'],
                    row['fuel_type'], row['price'], row['fetched_at'],
                ])

        dr.status = 'ready'
        dr.file_path = file_path
        dr.completed_at = timezone.now()
        dr.save(update_fields=['status', 'file_path', 'completed_at'])

    except Exception as exc:
        dr.status = 'error'
        dr.error_message = str(exc)
        dr.save(update_fields=['status', 'error_message'])


def cleanup_expired_downloads():
    """Remove download files older than 24 hours and mark as expired."""
    from dashboard.models import DownloadRequest

    cutoff = timezone.now() - timedelta(hours=24)
    expired_requests = DownloadRequest.objects.filter(
        status='ready',
        created_at__lt=cutoff,
    )
    for dr in expired_requests:
        if dr.file_path and os.path.exists(dr.file_path):
            try:
                os.remove(dr.file_path)
            except OSError:
                pass
        dr.status = 'expired'
        dr.save(update_fields=['status'])


def refresh_inspection_cache():
    """Fetch inspection data and save to InspectionCache."""
    from dashboard.models import InspectionCache

    data = get_inspection_data()
    InspectionCache.objects.create(data=data)
    # Keep only the latest cache entry
    latest = InspectionCache.objects.order_by('-created_at').first()
    if latest:
        InspectionCache.objects.exclude(pk=latest.pk).delete()
