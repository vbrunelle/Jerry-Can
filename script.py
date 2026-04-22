import requests
import os
import json

base_url = "http://localhost:8081"
login_url = f"{base_url}/admin/login/"
upload_chunk_url = f"{base_url}/api/upload/chunk/"
upload_complete_url = f"{base_url}/api/upload/complete/"
file_path = "/mnt/c/Users/vince/Downloads/analysis_1_1.json.gz"
chunk_size = 5 * 1024 * 1024  # 5MB

session = requests.Session()

# 1) Ouvre /admin/login/ pour obtenir csrftoken
print("Getting CSRF token...")
response = session.get(login_url)
csrftoken = session.cookies.get('csrftoken')
print(f"CSRF token: {csrftoken}")

# 2) Se connecte
print("Logging in...")
login_data = {
    "username": "lushlemur25",
    "password": "auHJGiDPbZENR4Q3",
    "csrfmiddlewaretoken": csrftoken
}
response = session.post(login_url, data=login_data, headers={"Referer": login_url})
print(f"Login status: {response.status_code}")

# 3) Upload en chunks
file_size = os.path.getsize(file_path)
upload_id = "test-upload-id-" + os.path.basename(file_path) # Example ID, might need to be dynamic
print(f"Uploading {file_path} ({file_size} bytes) in chunks...")

with open(file_path, "rb") as f:
    chunk_index = 0
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        
        files = {"file": (os.path.basename(file_path), chunk)}
        data = {
            "uploadId": upload_id,
            "chunkIndex": chunk_index,
            "totalChunks": (file_size + chunk_size - 1) // chunk_size
        }
        res = session.post(upload_chunk_url, files=files, data=data, headers={"X-CSRFToken": session.cookies.get('csrftoken')})
        if chunk_index % 5 == 0:
            print(f"Chunk {chunk_index} status: {res.status_code}")
        chunk_index += 1

# 4) Appelle /api/upload/complete/
print("Completing upload...")
complete_data = {"uploadId": upload_id}
response = session.post(upload_complete_url, json=complete_data, headers={"X-CSRFToken": session.cookies.get('csrftoken')})

# 5) Affiche status + body
print(f"Complete status: {response.status_code}")
print(f"Complete body: {response.text}")

