import logging
import os
import sqlite3
import sys
import argparse
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional Google Drive Imports
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

from database import export_manifest, init_database, is_uploaded, record_status
from notifications import (
    build_error_notification,
    build_idle_notification,
    build_success_notification,
)
from utils import (
    absolute_url,
    download_pdf_to_disk,
    find_moi_paper_universal,
    get_mdn_backup_papers,
    is_valid_pdf,
    MOI_SOURCES,
)

from ingest_engine import process_and_ingest_pdf

MAL_FOLDER_ID = os.environ.get("MAL_FOLDER_ID") or "1o1dwvEyIN-lqOTRSmRiT11oDbpy5M7rf"
KM_FOLDER_ID = os.environ.get("KM_FOLDER_ID") or "1cuClWkahxcWv39GvEUqy-k2Ou1jYgGfh"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "download_history.sqlite3")
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "download_manifest.json")
NEWSPAPER_SELECTION = os.environ.get("NEWSPAPER", "both").lower()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("my_reporter")

MMT_TZ = timezone(timedelta(hours=6, minutes=30))
DB_CONNECTION = None


def resolve_download_date(value):
    if not value:
        return datetime.now(MMT_TZ).date()
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as error:
        raise ValueError("DOWNLOAD_DATE must use YYYY-MM-DD format") from error


def save_history(
    *, newspaper, source, published_date, source_file_id, source_url,
    filename, status, drive_url=None, error=None
):
    if DB_CONNECTION is None:
        return
    record_status(
        DB_CONNECTION,
        newspaper=newspaper,
        source=source,
        published_date=published_date,
        source_file_id=source_file_id,
        source_url=source_url,
        filename=filename,
        status=status,
        drive_url=drive_url,
        error=error,
    )


def build_http_session():
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


HTTP_SESSION = build_http_session()


def send_telegram_message(message, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing!")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = HTTP_SESSION.post(url, json=payload, timeout=10)
        logger.info("Telegram notification sent, status=%s", res.status_code)
    except Exception as e:
        logger.error("Telegram notification failed: %s", e)


def get_gdrive_service():
    if not GDRIVE_AVAILABLE:
        logger.warning("Google Drive libraries missing. Skipping Drive upload.")
        return None

    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")

    if not (refresh_token and client_id and client_secret):
        logger.warning("GDRIVE Credentials Missing! Skipping Drive upload and continuing Direct DB Ingestion.")
        return None

    try:
        creds = Credentials(
            token=None, refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id, client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error("Failed to initialize Google Drive service: %s", e)
        return None


def file_exists_in_gdrive(service, folder_id, file_id_str):
    if not service: return False
    try:
        query = f"'{folder_id}' in parents and name contains '{file_id_str}' and trashed = false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        return len(results.get("files", [])) > 0
    except Exception as e:
        logger.error("Error checking file in Drive: %s", e)
        return False


def upload_to_gdrive(service, folder_id, local_file_path, filename):
    if not service: return None
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(local_file_path, mimetype="application/pdf", resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    file_id = file.get("id")
    web_link = file.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")
    logger.info("Uploaded to Drive: %s", filename)
    return web_link


def process_newspaper(service, folder_id, name, prefix, file_prefix, base_url, download_date):
    day = f"{download_date.day:02d}"
    month = f"{download_date.month:02d}"
    year = str(download_date.year)
    published_date = download_date.isoformat()
    filename = f"{download_date.strftime('%d-%b-%Y')}_{file_prefix}.pdf"

    file_url = find_moi_paper_universal(prefix, base_url, day, month, year)
    source_name = "moi"

    if not file_url:
        mdn_papers = get_mdn_backup_papers(day, month, year)
        for p in mdn_papers:
            if p["file_prefix"] == file_prefix or name in p["name"]:
                file_url = p["url"]
                source_name = "mdn"
                break

    if not file_url:
        return [], f"{name} အတွက် PDF ရှာမတွေ့ပါ"

    file_id = f"{file_prefix}_{day}_{month}_{year}"

    if DB_CONNECTION and is_uploaded(DB_CONNECTION, file_prefix, file_id):
        logger.info("Skipping database duplicate source=%s", file_prefix)
        return [], None

    if service and file_exists_in_gdrive(service, folder_id, file_id):
        logger.info("Skipping Drive duplicate source=%s", file_prefix)
        save_history(
            newspaper=file_prefix, source=source_name, published_date=published_date,
            source_file_id=file_id, source_url=file_url, filename=filename,
            status="skipped", error="Already exists in Google Drive",
        )
        return [], None

    local_path = os.path.join(tempfile.gettempdir(), filename)
    if not download_pdf_to_disk(file_url, local_path):
        error = "PDF download or verification failed"
        save_history(
            newspaper=file_prefix, source=source_name, published_date=published_date,
            source_file_id=file_id, source_url=file_url, filename=filename,
            status="failed", error=error,
        )
        return [], error

    try:
        drive_link = None
        if service:
            try:
                drive_link = upload_to_gdrive(service, folder_id, local_path, filename)
                save_history(
                    newspaper=file_prefix, source=source_name, published_date=published_date,
                    source_file_id=file_id, source_url=file_url, filename=filename,
                    status="uploaded", drive_url=drive_link,
                )
            except Exception as drive_err:
                logger.error("Drive upload failed: %s", drive_err)
        else:
            save_history(
                newspaper=file_prefix, source=source_name, published_date=published_date,
                source_file_id=file_id, source_url=file_url, filename=filename,
                status="downloaded", drive_url=None,
            )

        # Direct DB Ingestion
        ingest_ok = process_and_ingest_pdf(local_path, name, published_date)
        if ingest_ok:
            logger.info("Supabase direct ingestion success for %s", filename)
        else:
            logger.warning("Supabase direct ingestion skipped or failed for %s", filename)

        return [(filename, drive_link or "Direct DB Ingested")], None
    finally:
        if os.path.exists(local_path):
            try: os.remove(local_path)
            except Exception: pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD format")
    args, _ = parser.parse_known_args()

    requested_date_str = args.date or os.environ.get("DOWNLOAD_DATE")
    download_date = resolve_download_date(requested_date_str)

    init_database(DATABASE_PATH)
    DB_CONNECTION = sqlite3.connect(DATABASE_PATH)
    logger.info(
        "Starting download date=%s selection=%s database=%s",
        download_date.isoformat(), NEWSPAPER_SELECTION, DATABASE_PATH,
    )
    drive_service = get_gdrive_service()

    mal_uploads, err_mal = [], None
    km_uploads, err_km = [], None

    if NEWSPAPER_SELECTION in {"both", "myanma_alinn"}:
        mal_uploads, err_mal = process_newspaper(
            drive_service, MAL_FOLDER_ID, "မြန်မာ့အလင်း", "mal", "myanmaalinn", "https://www.moi.gov.mm", download_date
        )

    if NEWSPAPER_SELECTION in {"both", "kyemon"}:
        km_uploads, err_km = process_newspaper(
            drive_service, KM_FOLDER_ID, "ကြေးမုံ", "km", "themirror", "https://www.moi.gov.mm", download_date
        )

    all_uploads = mal_uploads + km_uploads
    export_manifest(DB_CONNECTION, MANIFEST_PATH)
    DB_CONNECTION.close()
    today_date = download_date.strftime("%d-%b-%Y")
    logger.info("Completed uploads=%d manifest=%s", len(all_uploads), MANIFEST_PATH)

    drive_urls = {
        "မြန်မာ့အလင်း": f"https://drive.google.com/drive/folders/{MAL_FOLDER_ID}",
        "ကြေးမုံ": f"https://drive.google.com/drive/folders/{KM_FOLDER_ID}",
    }
    if all_uploads:
        msg, buttons = build_success_notification(today_date, all_uploads, drive_urls)
        send_telegram_message(msg, buttons)
    elif err_mal or err_km:
        errors = []
        if err_mal: errors.append(("မြန်မာ့အလင်း", err_mal))
        if err_km: errors.append(("ကြေးမုံ", err_km))
        msg, buttons = build_error_notification(today_date, errors)
        send_telegram_message(msg, buttons)
    else:
        msg, buttons = build_idle_notification(today_date)
        send_telegram_message(msg, buttons)
    logger.info("Process completed")