import os
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Google Drive Folder IDs
MAL_FOLDER_ID = "1o1dwvEyIN-lqOTRSmRiT11oDbpy5M7rf"  # မြန်မာ့အလင်း
KM_FOLDER_ID = "1cuClWkahxcWv39GvEUqy-k2Ou1jYgGfh"   # ကြေးမုံ

# Telegram Settings
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        print(f"Telegram response code: {res.status_code}")
    except Exception as e:
        print(f"Telegram notification failed: {e}")

def get_gdrive_service():
    service_account_info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scopes)
    return build("drive", "v3", credentials=creds)

def file_exists_in_gdrive(service, folder_id, filename):
    query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return len(files) > 0

def upload_to_gdrive(service, folder_id, local_file_path, filename):
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaFileUpload(local_file_path, mimetype="application/pdf", resumable=True)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink"
    ).execute()
    file_id = file.get('id')
    web_link = file.get('webViewLink', f"https://drive.google.com/file/d/{file_id}/view")
    print(f"Uploaded: {filename}")
    return web_link

def process_newspaper(service, page_url, folder_id, prefix):
    print(f"\n--- Checking {prefix} from {page_url} ---")
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    uploaded_files = []

    try:
        resp = requests.get(page_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return uploaded_files

        soup = BeautifulSoup(resp.text, 'html.parser')
        links = soup.find_all('a', href=True)

        today_str = datetime.now().strftime("%d-%b-%Y")

        for a in links:
            href = a['href']
            if '/file-download/download/public/' in href or '.pdf' in href:
                file_url = href if href.startswith('http') else "https://www.moi.gov.mm" + href

                file_id = href.split('/')[-1].split('?')[0]
                filename = f"{today_str}_{prefix}_{file_id}.pdf"

                if file_exists_in_gdrive(service, folder_id, filename):
                    print(f"Skipping: {filename} (Already in Drive)")
                    continue

                print(f"Downloading {filename}...")
                file_resp = requests.get(file_url, headers=headers, timeout=60)
                if file_resp.status_code == 200:
                    local_path = f"/tmp/{filename}"
                    with open(local_path, 'wb') as f:
                        f.write(file_resp.content)

                    drive_link = upload_to_gdrive(service, folder_id, local_path, filename)
                    uploaded_files.append((filename, drive_link))

                    if os.path.exists(local_path):
                        os.remove(local_path)

    except Exception as e:
        print(f"Error: {e}")

    return uploaded_files

if __name__ == "__main__":
    drive_service = get_gdrive_service()
    
    mal_uploads = process_newspaper(drive_service, "https://www.moi.gov.mm/mal/", MAL_FOLDER_ID, "Myanma_Alinn")
    km_uploads = process_newspaper(drive_service, "https://www.moi.gov.mm/km/", KM_FOLDER_ID, "Kyemon")

    all_uploads = mal_uploads + km_uploads
    today_date = datetime.now().strftime("%d-%b-%Y")

    if all_uploads:
        msg = f"<b>📰 ယနေ့ ({today_date}) သတင်းစာအသစ်များ Google Drive သို့ သိမ်းဆည်းပြီးပါပြီ။</b>\n\n"
        for name, link in all_uploads:
            msg += f"• <a href='{link}'>{name}</a>\n"
        send_telegram_message(msg)
    else:
        msg = f"<b>📰 သတင်းစာ စနစ် စစ်ဆေးချက် ({today_date})</b>\n\nGoogle Drive ထဲတွင် ယနေ့ သတင်းစာများ အပြည့်အစုံ ရှိနှင့်ပြီး ဖြစ်ပါသည်။"
        send_telegram_message(msg)

    print("Process completed!")
