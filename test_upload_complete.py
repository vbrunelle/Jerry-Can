import requests
import os
import gzip
import uuid

BASE_URL = "http://localhost:8081"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
UPLOAD_URL = f"{BASE_URL}/api/upload/chunk"
COMPLETE_URL = f"{BASE_URL}/api/upload/complete"
TASKS_URL = f"{BASE_URL}/api/tasks"

# 1. Login
session = requests.Session()
login_payload = {"username": "admin", "password": "password"} # Assuming default password, might need adjustment
try:
    resp = session.post(LOGIN_URL, json=login_payload)
    print(f"Login status: {resp.status_code}")
except Exception as e:
    print(f"Login failed: {e}")
    exit(1)

# 2. Prepare gzip file
filename = "test_file.txt.gz"
filepath = os.path.join("/tmp", filename)
content = b"This is a small test gzip file content for chunked upload test."
with gzip.open(filepath, "wb") as f:
    f.write(content)

file_size = os.path.getsize(filepath)
upload_id = str(uuid.uuid4())

# 3. Upload in 2 chunks
with open(filepath, "rb") as f:
    chunk1 = f.read(file_size // 2)
    chunk2 = f.read()

for i, chunk in enumerate([chunk1, chunk2]):
    files = {'file': (filename, chunk)}
    data = {
        'upload_id': upload_id,
        'chunk_number': i,
        'total_chunks': 2,
        'filename': filename
    }
    resp = session.post(UPLOAD_URL, files=files, data=data)
    print(f"Chunk {i} status: {resp.status_code}")

# 4. Call complete
complete_payload = {
    'upload_id': upload_id,
    'filename': filename,
    'total_chunks': 2
}
resp = session.post(COMPLETE_URL, json=complete_payload)
print(f"Complete Status Code: {resp.status_code}")
print(f"Complete Response Text: {resp.text}")

# 5. Get last task
try:
    resp = session.get(TASKS_URL)
    tasks = resp.json()
    if isinstance(tasks, list) and len(tasks) > 0:
        # Assuming tasks are sorted or we take the last one
        # Filter for import tasks if possible, or just the latest
        last_task = tasks[-1]
        print(f"Last Task ID: {last_task.get('id')}")
        print(f"Last Task Status: {last_task.get('status')}")
    elif isinstance(tasks, dict) and 'items' in tasks:
        last_task = tasks['items'][-1]
        print(f"Last Task ID: {last_task.get('id')}")
        print(f"Last Task Status: {last_task.get('status')}")
    else:
        print(f"Tasks response: {tasks}")
except Exception as e:
    print(f"Error fetching tasks: {e}")

# Cleanup
if os.path.exists(filepath):
    os.remove(filepath)
