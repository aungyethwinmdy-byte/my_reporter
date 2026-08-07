import json
import os
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import requests

# Google Drive Folder IDs
MAL_FOLDER_ID = "1o1dwvEyIN-lqOTRSmRiT11oDbpy5M7rf"  # မြန်မာ့အလင်း
KM_FOLDER_ID = "1cuClWkahxcWv39GvEUqy-k2Ou1jYgGfh"  # ကြေးမုံ

# Telegram Settings
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# မြန်မာစံတော်ချိန် (UTC + 6:30)
MMT_TZ = timezone(timedelta(hours=6, minutes=30))


def send_telegram_message(message):
  if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Telegram credentials missing!")
    return
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {
      "chat_id": TELEGRAM_CHAT_ID,
      "text": message,
      "parse_mode": "HTML",
      "disable_web_page_preview": False,
  }
  try:
    res = requests.post(url, json=payload, timeout=10)
    print(f"Telegram response code: {res.status_code}")
  except Exception as e:
    print(f"Telegram notification failed: {e}")


def get_gdrive_service():
  service_account_info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_KEY"])
  scopes = ["https://www.googleapis.com/auth/drive.file"]
  creds = Credentials.from_service_account_info(
      service_account_info, scopes=scopes
  )
  return build("drive", "v3", credentials=creds)


def file_exists_in_gdrive(service, folder_id, file_id_str):
  query = (
      f"'{folder_id}' in parents and name contains '{file_id_str}' and trashed ="
      " false"
  )
  results = service.files().list(q=query, fields="files(id, name)").execute()
  files = results.get("files", [])
  return len(files) > 0


def upload_to_gdrive(service, folder_id, local_file_path, filename):
  file_metadata = {"name": filename, "parents": [folder_id]}
  media = MediaFileUpload(
      local_file_path, mimetype="application/pdf", resumable=True
  )
  file = (
      service.files()
      .create(
          body=file_metadata, media_body=media, fields="id, webViewLink"
      )
      .execute()
  )
  file_id = file.get("id")
  web_link = file.get(
      "webViewLink", f"https://drive.google.com/file/d/{file_id}/view"
  )
  print(f"Uploaded: {filename}")
  return web_link


# ၁။ MOI ဝက်ဘ်ဆိုက်မှ သတင်းစာ စစ်ဆေးရယူခြင်း
def process_newspaper_moi(service, page_url, folder_id, prefix):
  print(f"\n--- Checking MOI ({prefix}) from {page_url} ---")
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  uploaded_files = []

  try:
    resp = requests.get(page_url, headers=headers, timeout=30)
    if resp.status_code != 200:
      print(f"Failed to fetch MOI {page_url}, status: {resp.status_code}")
      return uploaded_files, f"MOI ({prefix}) ဝင်မရပါ"

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=True)

    today = datetime.now(MMT_TZ)
    today_str = today.strftime("%d-%b-%Y")

    for a in links:
      href = a["href"]
      if "/file-download/download/public/" in href or ".pdf" in href:
        file_url = (
            href
            if href.startswith("http")
            else "https://www.moi.gov.mm" + href
        )
        file_id = href.split("/")[-1].split("?")[0]

        if file_exists_in_gdrive(service, folder_id, file_id):
          print(f"Skipping: {file_id} (Already in Drive)")
          continue

        filename = f"{today_str}_{prefix}_{file_id}.pdf"
        print(f"Downloading {filename} from MOI {file_url}...")

        file_resp = requests.get(file_url, headers=headers, timeout=60)
        if file_resp.status_code == 200:
          local_path = f"/tmp/{filename}"
          with open(local_path, "wb") as f:
            f.write(file_resp.content)

          drive_link = upload_to_gdrive(
              service, folder_id, local_path, filename
          )
          uploaded_files.append((filename, drive_link))

          if os.path.exists(local_path):
            os.remove(local_path)

  except Exception as e:
    print(f"MOI Error in {prefix}: {e}")
    return uploaded_files, str(e)

  return uploaded_files, None


# ၂။ MOI တွင် မတွေ့ပါက MDN Newspaper Portal မှ အပိုဆောင်း စစ်ဆေးရယူခြင်း (Fallback)
def process_newspaper_mdn_fallback(service, folder_id, prefix, paper_type):
  print(f"\n--- Checking MDN Fallback for {prefix} ({paper_type}) ---")
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
      )
  }

  uploaded_files = []
  today = datetime.now(MMT_TZ)
  today_str = today.strftime("%d-%b-%Y")

  # MDN Date format e.g. 7-8-2026
  mdn_date_str = f"{today.day}-{today.month}-{today.year}"
  mdn_url = f"https://www.mdn.gov.mm/newspaper/public/?published_date={mdn_date_str}"

  try:
    resp = requests.get(mdn_url, headers=headers, timeout=30)
    if resp.status_code != 200:
      print(f"MDN page failed: {mdn_url}")
      return uploaded_files, f"MDN Portal ({mdn_date_str}) ဝင်မရပါ"

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=True)

    file_id_key = f"MDN_{paper_type}_{mdn_date_str}"

    if file_exists_in_gdrive(service, folder_id, file_id_key):
      print(f"Skipping MDN: {file_id_key} (Already in Drive)")
      return uploaded_files, None

    for a in links:
      href = a["href"]
      if paper_type in href.lower() or ".pdf" in href or "download" in href:
        file_url = (
            href if href.startswith("http") else "https://www.mdn.gov.mm" + href
        )

        filename = f"{today_str}_{prefix}_{file_id_key}.pdf"
        print(f"Downloading {filename} from MDN {file_url}...")

        file_resp = requests.get(file_url, headers=headers, timeout=60)
        if file_resp.status_code == 200:
          local_path = f"/tmp/{filename}"
          with open(local_path, "wb") as f:
            f.write(file_resp.content)

          drive_link = upload_to_gdrive(
              service, folder_id, local_path, filename
          )
          uploaded_files.append((filename, drive_link))

          if os.path.exists(local_path):
            os.remove(local_path)
          break

  except Exception as e:
    print(f"MDN Fallback Error in {prefix}: {e}")
    return uploaded_files, str(e)

  return uploaded_files, None


if __name__ == "__main__":
  drive_service = get_gdrive_service()

  # ၁။ MOI ဝက်ဘ်ဆိုက်ကို အရင် စစ်ဆေးခြင်း
  mal_uploads, err_mal = process_newspaper_moi(
      drive_service,
      "https://www.moi.gov.mm/mal/",
      MAL_FOLDER_ID,
      "Myanma_Alinn",
  )
  km_uploads, err_km = process_newspaper_moi(
      drive_service, "https://www.moi.gov.mm/km/", KM_FOLDER_ID, "Kyemon"
  )

  # ၂။ MOI တွင် ဖိုင်သစ် မတွေ့ပါက MDN Portal သို့ အလိုအလျောက် သွားရောက် စစ်ဆေးခြင်း
  if not mal_uploads:
    mal_mdn, err_mdn_mal = process_newspaper_mdn_fallback(
        drive_service, MAL_FOLDER_ID, "Myanma_Alinn", "mal"
    )
    mal_uploads.extend(mal_mdn)

  if not km_uploads:
    km_mdn, err_mdn_km = process_newspaper_mdn_fallback(
        drive_service, KM_FOLDER_ID, "Kyemon", "km"
    )
    km_uploads.extend(km_mdn)

  all_uploads = mal_uploads + km_uploads
  today_date = datetime.now(MMT_TZ).strftime("%d-%b-%Y")

  # Google Drive ဖိုဒါ လင့်ခ်များ
  mal_drive_url = f"https://drive.google.com/drive/folders/{MAL_FOLDER_ID}"
  km_drive_url = f"https://drive.google.com/drive/folders/{KM_FOLDER_ID}"

  if all_uploads:
    msg = (
        f"<b>📰 ယနေ့ ({today_date}) သတင်းစာအသစ်များ Google Drive သို့"
        " သိမ်းဆည်းပြီးပါပြီ။</b>\n\n"
    )
    for name, link in all_uploads:
      msg += f"• <a href='{link}'>{name}</a>\n"

    msg += "\n<b>📁 Google Drive ဖိုဒါများ -</b>\n"
    msg += f"• <a href='{mal_drive_url}'>မြန်မာ့အလင်း ဖိုဒါ ကြည့်ရန်</a>\n"
    msg += f"• <a href='{km_drive_url}'>ကြေးမုံ ဖိုဒါ ကြည့်ရန်</a>\n"

    send_telegram_message(msg)
  elif err_mal or err_km:
    msg = (
        f"<b>⚠️ သတင်းစာ စနစ် စစ်ဆေးချက် ({today_date})</b>\n\nဝက်ဘ်ဆိုက်မှ သတင်းစာ"
        " ရယူစဉ် အမှားတက်ခဲ့ပါသည်:\n"
    )
    if err_mal:
      msg += f"• မြန်မာ့အလင်း: {err_mal}\n"
    if err_km:
      msg += f"• ကြေးမုံ: {err_km}\n"

    msg += "\n<b>📁 Google Drive ဖိုဒါများ -</b>\n"
    msg += f"• <a href='{mal_drive_url}'>မြန်မာ့အလင်း ဖိုဒါ ကြည့်ရန်</a>\n"
    msg += f"• <a href='{km_drive_url}'>ကြေးမုံ ဖိုဒါ ကြည့်ရန်</a>\n"

    send_telegram_message(msg)
  else:
    msg = (
        f"<b>📰 သတင်းစာ စနစ် စစ်ဆေးချက် ({today_date})</b>\n\nGoogle Drive ထဲတွင်"
        " ယနေ့ သတင်းစာများ ရှိနှင့်ပြီး ဖြစ်ပါသည် (သို့မဟုတ်) ဝက်ဘ်ဆိုက်တွင်"
        " သတင်းစာသစ် မထွက်သေးပါ။\n\n"
    )
    msg += "<b>📁 Google Drive ဖိုဒါများ -</b>\n"
    msg += f"• <a href='{mal_drive_url}'>မြန်မာ့အလင်း ဖိုဒါ ကြည့်ရန်</a>\n"
    msg += f"• <a href='{km_drive_url}'>ကြေးမုံ ဖိုဒါ ကြည့်ရန်</a>\n"

    send_telegram_message(msg)

  print("Process completed!")
