import os
import sys
import json
import gzip
import django
import time
from pathlib import Path
from io import StringIO
from datetime import datetime

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jerrycan.settings')
os.environ['DJANGO_DB_DIR'] = '/tmp'
sys.path.insert(0, '/mnt/c/Users/vince/Prog/Jerry-Can/jerrycan')

django.setup()

from prices.models import Analysis, Fuel, Station, Snapshot, Price, AnalysisTransferTask
from django.db import transaction

# Timing dict
timings = {}

# Load the test file
test_file = Path('/mnt/c/Users/vince/Downloads/analysis_1_1.json.gz')
if not test_file.exists():
    print(f"File not found: {test_file}")
    sys.exit(1)

print(f"Starting benchmark on: {test_file}")
print(f"File size: {test_file.stat().st_size / (1024**2):.1f} MB")
print("-" * 60)

try:
    # Phase 1: JSON read
    t0 = time.time()
    with gzip.open(test_file, 'rt', encoding='utf-8') as f:
        data = json.load(f)
    timings['JSON_read'] = time.time() - t0
    print(f"[Phase 1] JSON read: {timings['JSON_read']:.2f}s")
    print(f"  - Snapshots in file: {len(data.get('snapshots', []))}")
    print(f"  - Stations in file: {len(data.get('stations', []))}")
    print(f"  - Prices in file: {len(data.get('prices', []))}")
    print(f"  - Fuels in file: {len(data.get('fuels', []))}")

    with transaction.atomic():
        # Phase 2: Analysis creation - Adjusted FIELDS
        t0 = time.time()
        analysis = Analysis.objects.create(
            active=False,
            # name and description don't exist in model
        )
        timings['Analysis_create'] = time.time() - t0
        print(f"[Phase 2] Analysis create: {timings['Analysis_create']:.2f}s")

        # Phase 3: Fuels
        t0 = time.time()
        for fuel_data in data.get('fuels', []):
            Fuel.objects.get_or_create(
                name=fuel_data['name'],
                defaults={'description': fuel_data.get('description', '')}
            )
        timings['Fuels_loop'] = time.time() - t0
        print(f"[Phase 3] Fuels loop: {timings['Fuels_loop']:.2f}s")

        # Phase 4: Stations bulk_create
        t0 = time.time()
        stations = []
        stations_by_id = {}
        for station_data in data.get('stations', []):
            station = Station(
                station_id=station_data['station_id'],
                name=station_data['name'],
                region=station_data.get('region', ''),
                latitude=float(station_data.get('latitude', 0)),
                longitude=float(station_data.get('longitude', 0))
            )
            stations.append(station)
            stations_by_id[station_data['station_id']] = station
        
        BATCH_SIZE = 2000
        for i in range(0, len(stations), BATCH_SIZE):
            Station.objects.bulk_create(stations[i:i+BATCH_SIZE], ignore_conflicts=True)
        timings['Stations_bulk_create'] = time.time() - t0
        print(f"[Phase 4] Stations bulk_create ({len(stations)} items): {timings['Stations_bulk_create']:.2f}s")

        # Reload stations from DB for foreign keys
        stations_in_db = {s.station_id: s for s in Station.objects.all()}

        # Phase 5: Snapshots bulk_create
        t0 = time.time()
        snapshots = []
        snapshots_temp_storage = []
        for snapshot_data in data.get('snapshots', []):
            station = stations_in_db.get(snapshot_data['station_id'])
            if station:
                snapshot = Snapshot(
                    analysis=analysis,
                    station=station,
                    capture_datetime=snapshot_data['capture_datetime'],
                    import_status='imported'
                )
                snapshots.append(snapshot)
                snapshots_temp_storage.append((snapshot_data['snapshot_id'], snapshot))
        
        for i in range(0, len(snapshots), BATCH_SIZE):
            Snapshot.objects.bulk_create(snapshots[i:i+BATCH_SIZE], batch_size=BATCH_SIZE)
        timings['Snapshots_bulk_create'] = time.time() - t0
        print(f"[Phase 5] Snapshots bulk_create ({len(snapshots)} items): {timings['Snapshots_bulk_create']:.2f}s")

        # Phase 6: Timestamp patch (now using bulk_update if needed, 
        # but bulk_create might already have handled it if capture_datetime is not auto_now)
        # We need the IDs back from DB.
        t0 = time.time()
        # Fetching back to get IDs
        snapshots_in_db = {s.capture_datetime.isoformat(): s for s in Snapshot.objects.filter(analysis=analysis)}
        
        # Link snapshot_id from JSON to DB object
        snapshots_by_json_id = {}
        for json_sid, snap_obj in snapshots_temp_storage:
             # This is a bit fragile if multiple snapshots have exact same timestamp for same analysis
             # but it's for benchmarking.
             key = snap_obj.capture_datetime
             if isinstance(key, str):
                 key_str = key.replace('Z', '+00:00')
             else:
                 key_str = key.isoformat()
             
             # Attempt to find exactly matching record
             for s_db in Snapshot.objects.filter(analysis=analysis, capture_datetime=key):
                 snapshots_by_json_id[json_sid] = s_db
                 break

        timings['Snapshots_matching'] = time.time() - t0
        print(f"[Phase 6] Snapshots matching: {timings['Snapshots_matching']:.2f}s")

        # Phase 7: Prices bulk_create
        t0 = time.time()
        prices = []
        fuels_cache = {f.name: f for f in Fuel.objects.all()}
        
        for price_data in data.get('prices', []):
            fuel = fuels_cache.get(price_data['fuel_name'])
            snapshot = snapshots_by_json_id.get(price_data['snapshot_id'])
            if snapshot and fuel:
                price = Price(
                    snapshot=snapshot,
                    fuel=fuel,
                    price=float(price_data['price'])
                )
                prices.append(price)
        
        BATCH_PRICES = 10000
        for i in range(0, len(prices), BATCH_PRICES):
            Price.objects.bulk_create(prices[i:i+BATCH_PRICES], batch_size=BATCH_PRICES)
        timings['Prices_bulk_create'] = time.time() - t0
        print(f"[Phase 7] Prices bulk_create ({len(prices)} items): {timings['Prices_bulk_create']:.2f}s")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Summary
print("-" * 60)
print("TIMING SUMMARY:")
total = sum(timings.values())
for phase, duration in sorted(timings.items(), key=lambda x: x[1], reverse=True):
    pct = (duration / total) * 100
    print(f"  {phase:30s}: {duration:8.2f}s ({pct:5.1f}%)")
print(f"  {'TOTAL':30s}: {total:8.2f}s")
