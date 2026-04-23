import requests
import os
import gzip
import uuid

BASE_URL = "http://localhost:8081"
# Found path: path('accounts/login/', auth_views.LoginView.as_view(), name='login')
LOGIN_URL = f"{BASE_URL}/accounts/login/"
# Found path: path("api/upload/chunk/", views.upload_chunk, name="upload_chunk")
UPLOAD_URL = f"{BASE_URL}/api/upload/chunk/"
# Found path: path("api/upload/complete/", views.upload_complete, name="upload_complete")
COMPLETE_URL = f"{BASE_URL}/api/upload/complete/"
# Re-checking tasks; it might be different, let's look for status API
# path("api/transfer/<int:task_id>/status/", ...
# Let's try to find a list of tasks or just rely on what we can find.

session = requests.Session()

# Get CSRF token first
session.get(LOGIN_URL)
csrf_token = session.cookies.get('csrftoken')

# 1. Login
# Django login form usually uses 'username', 'password' and 'csrfmiddlewaretoken'
login_payload = {
    "username": "admin",
    "password": "password",
    "csrfmiddlewaretoken": csrf_token
}
resp = session.post(LOGIN_URL, data=login_payload, headers={"Referer": LOGIN_URL})
print(f"Login status: {resp.status_code}")

# Get new CSRF token after login
csrf_token = session.cookies.get('csrftoken')

# 2. Prepare file
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
        'filename': filename,
        'csrfmiddlewaretoken': csrf_token
    }
    resp = session.post(UPLOAD_URL, files=files, data=data, headers={"X-CSRFToken": csrf_token, "Referer": BASE_URL})
    print(f"Chunk {i} status: {resp.status_code}")

# 4. Call complete
complete_payload = {
    'upload_id': upload_id,
    'filename': filename,
    'total_chunks': 2
}
# Using JSON but might need CSRF in header
resp = session.post(COMPLETE_URL, json=complete_payload, headers={"X-CSRFToken": csrf_token, "Referer": BASE_URL})
print(f"Complete Status Code: {resp.status_code}")
print(f"Complete Response Text: {resp.text}")

# 5. Get many tasks or last one if possible
# Since we don't have the exact "tasks list" URL, we'll try to guess or search.
# But I will try /api/transfer/1/status/ just to see if it exists
status_url = f"{BASE_URL}/api/transfer/1/status/"
resp = session.get(status_url)
print(f"Task 1 status: {resp.status_code} {resp.text}")

if os.path.exists(filepath):
    os.remove(filepath)
