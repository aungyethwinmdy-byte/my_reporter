import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive Folder IDs
MAL_FOLDER_ID = "1o1dwvEyIN-lqOTRSmRiT11oDbpy5M7rf"  # မြန်မာ့အလင်း Folder
KM_FOLDER_ID = "1cuClWkahxcWv39GvEUqy-k2Ou1jYgGfh"   # ကြေးမုံ Folder

# Google Drive API Authentication
def get_gdrive_service():
    service_account_info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    return build("drive", "v3", credentials=creds)

# Drive တွင် ဖိုင် ရှိနှင့်ပြီးဖြစ်သလား စစ်ဆေးခြင်း
def file_exists_in_gdrive(service, folder_id, filename):
    query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return len(files) > 0

# Google Drive သို့ Upload တင်ခြင်း
def upload_to_gdrive(service, folder_id, local_file_path, filename):
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaFileUpload(local_file_path, mimetype="application/pdf", resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    print(f"Uploaded successfully. File ID: {file.get('id')}")

# သတင်းစာများ ဒေါင်းလုဒ်ဆွဲခြင်း
def process_newspaper(service, page_url, folder_id, prefix):
    print(f"\n--- Checking {prefix} from {page_url} ---")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    try:
        resp = requests.get(page_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"Failed to access {page_url}")
            return

        soup = BeautifulSoup(resp.text, 'html.parser')
        links = soup.find_all('a', href=True)

        today_str = datetime.now().strftime("%Y-%m-%d")

        for a in links:
            href = a['href']
            if '/file-download/download/public/' in href or '.pdf' in href:
                file_url = href if href.startswith('http') else "https://www.moi.gov.mm" + href

                file_id = href.split('/')[-1].split('?')[0]
                filename = f"{today_str}_{prefix}_{file_id}"
                if not filename.endswith('.pdf'):
                    filename += ".pdf"

                # Drive ထဲတွင် ရှိပြီးသားဖြစ်ပါက Skip လုပ်မည်
                if file_exists_in_gdrive(service, folder_id, filename):
                    print(f"Skipping: {filename} (Already in Google Drive)")
                    continue

                print(f"Downloading {filename}...")
                file_resp = requests.get(file_url, headers=headers, timeout=60)
                if file_resp.status_code == 200:
                    local_path = f"/tmp/{filename}"
                    with open(local_path, 'wb') as f:
                        f.write(file_resp.content)

                    upload_to_gdrive(service, folder_id, local_path, filename)

                    if os.path.exists(local_path):
                        os.remove(local_path)
                else:
                    print(f"Failed to download {filename}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    drive_service = get_gdrive_service()
    process_newspaper(drive_service, "https://www.moi.gov.mm/mal/", MAL_FOLDER_ID, "Myanma_Alinn")
    process_newspaper(drive_service, "https://www.moi.gov.mm/km/", KM_FOLDER_ID, "Kyemon")
