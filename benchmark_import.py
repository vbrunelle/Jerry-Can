import os
import sys
import time
import gzip
import json
import tempfile
import django
from django.conf import settings
from django.utils import timezone
from datetime import datetime

# Setup Django environment
sys.path.append('/mnt/c/Users/vince/Prog/Jerry-Can/jerrycan')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'jerrycan.settings')

# Use a temporary SQLite database
temp_db_dir = tempfile.mkdtemp()
db_path = os.path.join(temp_db_dir, 'benchmark.sqlite3')
os.environ['DJANGO_DB_DIR'] = temp_db_dir

# Initialize Django
django.setup()

from django.db import transaction, connection
from prices.models import Analysis, Fuel, Station, Snapshot, Price

# Monkeypatching for timing
class Timer:
    def __init__(self):
        self.timings = {}

    def record(self, key, duration):
        self.timings[key] = self.timings.get(key, 0) + duration

timer = Timer()

def patch_bulk_create(model_class, key):
    original_bulk_create = model_class.objects.bulk_create
    def timed_bulk_create(*args, **kwargs):
        start = time.time()
        result = original_bulk_create(*args, **kwargs)
        timer.record(f"db_bulk_create_{key}", time.time() - start)
        return result
    model_class.objects.bulk_create = timed_bulk_create

patch_bulk_create(Station, "stations")
patch_bulk_create(Snapshot, "snapshots")
patch_bulk_create(Price, "prices")

original_bulk_update = Snapshot.objects.bulk_update
def timed_bulk_update(*args, **kwargs):
    start = time.time()
    result = original_bulk_update(*args, **kwargs)
    timer.record("db_bulk_update_snapshots_timestamps", time.time() - start)
    return result
Snapshot.objects.bulk_update = timed_bulk_update

def run_benchmark(filepath):
    print(f"Starting benchmark for {filepath}")
    total_start = time.time()

    # 1. Read / Decompress JSON
    print("Step 1: JSON Load...")
    start = time.time()
    with gzip.open(filepath, 'rt', encoding='utf-8') as f:
        # Avoid loading everything at once if possibly huge, but here we use json.load
        data = json.load(f)
    timer.record("json_load_decompress", time.time() - start)

    # 2. Preparation & Analysis Creation
    print("Step 2: Analysis Creation...")
    start = time.time()
    analysis_data = data.get('analysis', {})
    with transaction.atomic():
        analysis = Analysis(
            update_frequency=analysis_data.get('update_frequency', 5),
            data_source_url=analysis_data.get('data_source_url', ''),
            active=analysis_data.get('active', False),
            run_automatically=False,
            max_snapshots=analysis_data.get('max_snapshots', 100),
        )
        analysis.save()
    timer.record("create_analysis", time.time() - start)

    # 3. Fuels
    print("Step 3: Fuels...")
    start = time.time()
    fuel_id_map = {}
    for fuel_data in data.get('fuels', []):
        fuel, _ = Fuel.objects.get_or_create(name=fuel_data['name'])
        fuel_id_map[fuel_data['id']] = fuel.pk
    timer.record("process_fuels", time.time() - start)

    # 4. Stations
    print("Step 4: Stations...")
    start = time.time()
    station_id_map = {}
    station_objs = []
    station_data_list = data.get('stations', [])
    for s in station_data_list:
        station_objs.append(Station(
            name=s['name'],
            city=s['city'],
            region=s['region'],
            adress=s.get('adress', ''),
            longitude=s.get('longitude'),
            latitude=s.get('latitude'),
            analysis=analysis,
        ))
    
    BATCH = 500 # Smaller batch to be safer
    idx = 0
    for i in range(0, len(station_objs), BATCH):
        chunk = station_objs[i:i + BATCH]
        created = Station.objects.bulk_create(chunk)
        for j, obj in enumerate(created):
            old_id = station_data_list[idx]['id']
            station_id_map[old_id] = obj.pk
            idx += 1
    timer.record("process_stations", time.time() - start)

    # 5. Snapshots
    print("Step 5: Snapshots...")
    start = time.time()
    snapshot_id_map = {}
    snapshot_objs = []
    snapshot_data_list = data.get('snapshots', [])
    for snap in snapshot_data_list:
        snapshot_objs.append(Snapshot(
            analysis=analysis,
            status=snap.get('status', Snapshot.Status.PROCESSED),
        ))

    created_snapshots = []
    idx = 0
    for i in range(0, len(snapshot_objs), BATCH):
        chunk = snapshot_objs[i:i + BATCH]
        created = Snapshot.objects.bulk_create(chunk)
        created_snapshots.extend(created)
        for j, obj in enumerate(created):
            old_id = snapshot_data_list[idx]['id']
            snapshot_id_map[old_id] = obj.pk
            idx += 1
    timer.record("process_snapshots_creation", time.time() - start)

    # 6. Timestamp Patch
    print("Step 6: Timestamp Patch...")
    start = time.time()
    for obj, snap in zip(created_snapshots, snapshot_data_list):
        ts_str = snap['timestamp']
        ts_val = datetime.fromisoformat(ts_str)
        if timezone.is_aware(timezone.now()):
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
    timer.record("process_snapshots_timestamps", time.time() - start)

    # 7. Prices
    print("Step 7: Prices...")
    start = time.time()
    price_batch = []
    prices_data = data.get('prices', [])
    for pr in prices_data:
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
    timer.record("process_prices", time.time() - start)

    total_duration = time.time() - total_start
    
    print("\n--- Benchmark Results ---")
    print(f"Total time: {total_duration:.2f}s")
    for k, v in timer.timings.items():
        print(f"{k}: {v:.4f}s")
    
    print("\n--- Volumes ---")
    print(f"Stations: {len(station_data_list)}")
    print(f"Snapshots: {len(snapshot_data_list)}")
    print(f"Prices: {len(prices_data)}")

    # Cleanup
    import shutil
    try:
        shutil.rmtree(temp_db_dir)
    except:
        pass

if __name__ == "__main__":
    filepath = "/mnt/c/Users/vince/Downloads/analysis_1_1.json.gz"
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        sys.exit(1)
    
    from django.core.management import call_command
    call_command('migrate', verbosity=0)
    
    run_benchmark(filepath)
