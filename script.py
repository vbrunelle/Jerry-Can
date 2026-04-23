import json
import os

path = '/tmp/export.json'
if not os.path.exists(path):
    print(f"Error: {path} not found")
else:
    # Use ijson or similar if file is too big, but let's try reading lines or a different approach
    # The file is 1.7GB, might fit in RAM but let's be careful.
    with open(path) as f:
        # Assuming the JSON structure is as expected, otherwise use a streaming parser
        data = json.load(f)
        snaps = data.get('snapshots', [])
        missing_ts = [s for s in snaps if 'timestamp' not in s or s.get('timestamp') is None]
        print(f'Snapshots missing timestamp: {len(missing_ts)} / {len(snaps)}')
        if missing_ts:
            print('Examples of missing timestamp:')
            for s in missing_ts[:3]:
                print(f'  {s}')
        else:
            print('All snapshots have timestamp field')
            if snaps:
                print(f'Sample: {snaps[0]}')
