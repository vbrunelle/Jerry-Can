import requests
import os
import math
import uuid

BASE_URL = "http://localhost:8081"
LOGIN_URL = f"{BASE_URL}/accounts/login/"
CHUNK_URL = f"{BASE_URL}/api/upload/chunk/"
COMPLETE_URL = f"{BASE_URL}/api/upload/complete/"
FILE_PATH = "/mnt/c/Users/vince/Downloads/analysis_1_1.json.gz"
CHUNK_SIZE = 5 * 1024 * 1024

session = requests.Session()
login_page = session.get(LOGIN_URL)
csrf_token = session.cookies.get('csrftoken')

payload = {
    'username': 'lushlemur25',
    'password': 'password123',
    'csrfmiddlewaretoken': csrf_token
}
r_login = session.post(LOGIN_URL, data=payload, headers={'Referer': LOGIN_URL})
print(f"Login status: {r_login.status_code}")

file_name = os.path.basename(FILE_PATH)
file_size = os.path.getsize(FILE_PATH)
total_chunks = math.ceil(file_size / CHUNK_SIZE)
upload_id = str(uuid.uuid4())

with open(FILE_PATH, 'rb') as f:
    for i in range(total_chunks):
        chunk_data = f.read(CHUNK_SIZE)
        headers = {
            'X-CSRFToken': session.cookies.get('csrftoken'),
            'Referer': BASE_URL
        }
        files = {'chunk': (file_name, chunk_data)}
        data = {
            'uploadId': upload_id,
            'chunkNumber': i,
            'totalChunks': total_chunks,
        }
        r = session.post(CHUNK_URL, data=data, files=files, headers=headers)
        print(f"Chunk {i} status: {r.status_code}. Response: {r.text[:100]}")
        if r.status_code != 200:
            break

if r.status_code == 200:
    r_complete = session.post(COMPLETE_URL, data={'uploadId': upload_id}, headers={'X-CSRFToken': session.cookies.get('csrftoken'), 'Referer': BASE_URL})
    print(f"Complete status: {r_complete.status_code}")
    print(r_complete.text)
