import requests
import os
import gzip
import uuid
from bs4 import BeautifulSoup

BASE_URL = "http://localhost:8081"
LOGIN_URL = f"{BASE_URL}/admin/login/"
UPLOAD_URL = f"{BASE_URL}/api/upload/chunk/"
COMPLETE_URL = f"{BASE_URL}/api/upload/complete/"

session = requests.Session()

# 1. Get CSRF from login page
resp = session.get(LOGIN_URL)
soup = BeautifulSoup(resp.text, 'html.parser')
csrf_token = soup.find('input', {'name': 'csrfmiddlewaretoken'})['value']

# 2. Login
login_payload = {
    "username": "admin",
    "password": "password",
    "csrfmiddlewaretoken": csrf_token,
    "next": "/admin/"
}
resp = session.post(LOGIN_URL, data=login_payload, headers={"Referer": LOGIN_URL})
print(f"Login status: {resp.status_code}")

# Check if we are really logged in
resp = session.get(f"{BASE_URL}/admin/")
if "Log in" in resp.text:
    print("Login failed - still showing login page")
else:
    print("Login successful")

# Get updated CSRF token
csrf_token = session.cookies.get('csrftoken')

# 3. Prepare file
filename = "test_file.txt.gz"
filepath = os.path.join("/tmp", filename)
content = b"This is a small test gzip file content for chunked upload test."
with gzip.open(filepath, "wb") as f:
    f.write(content)

file_size = os.path.getsize(filepath)
upload_id = str(uuid.uuid4())

# 4. Upload in 2 chunks
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
    resp = session.post(UPLOAD_URL, files=files, data=data, headers={"X-CSRFToken": csrf_token, "Referer": BASE_URL})
    print(f"Chunk {i} status: {resp.status_code}")

# 5. Call complete
complete_payload = {
    'upload_id': upload_id,
    'filename': filename,
    'total_chunks': 2
}
resp = session.post(COMPLETE_URL, json=complete_payload, headers={"X-CSRFToken": csrf_token, "Referer": BASE_URL})
print(f"Complete Status Code: {resp.status_code}")
print(f"Complete Response Text: {resp.text}")

# 6. Try to find the last task status (we'll guess task ID might be high or we look for recent ones)
# Since we don't know the task ID, we can't easily query /api/transfer/<id>/status/ 
# unless the complete response gives it to us.
