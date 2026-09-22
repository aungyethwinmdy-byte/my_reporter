import logging
import os
import sqlite3
import argparse
import tempfile
from datetime import datetime, timedelta, timezone

# Load .env before importing anything local. Several local modules read the
# environment at import time and freeze the result (utils.VERIFY_TLS is the one
# that bit us), so an entry point must establish the environment itself rather
# than inherit whichever import line happened to pull in dotenv. The module
# constants below (MAL_FOLDER_ID, TELEGRAM_*, NEWSPAPER) are read at import time
# for the same reason.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # Optional: on CI the values come from the environment directly.
    pass

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
    download_pdf_to_disk,
    find_moi_paper_universal,
    get_mdn_backup_papers,
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


def set_database_connection(connection):
    """Bind the module-level history connection.

    ``__main__`` cannot assign this directly: a bare ``DB_CONNECTION = ...``
    inside the ``if __name__ == "__main__":`` block creates a *new local* in
    the ``__main__`` module, leaving ``main.DB_CONNECTION`` as ``None``. Every
    ``save_history()`` call then hit its early ``if DB_CONNECTION is None:
    return`` and silently discarded the row, so ``is_uploaded()`` never saw a
    history entry and the pipeline re-downloaded and re-uploaded the same issue
    on every run. Route the assignment through this function instead.
    """
    global DB_CONNECTION
    DB_CONNECTION = connection
    return DB_CONNECTION


def close_database_connection():
    """Commit and close the history connection if one is bound."""
    global DB_CONNECTION
    if DB_CONNECTION is not None:
        try:
            DB_CONNECTION.close()
        finally:
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


def _drive_query_literal(value):
    """Return *value* as a quoted Drive ``q`` literal, or ``None`` if unsafe.

    Drive's query language has no parameter binding — values are interpolated
    between single quotes, so an embedded quote would end the literal and
    silently change the meaning of the query. Backslash is its escape
    character, so it is rejected for the same reason. Names we build cannot
    contain either; such a value is a programming error, and guessing at an
    escape is how query injection starts.
    """
    if value is None:
        return None
    text = str(value)
    if not text or "'" in text or "\\" in text:
        return None
    return f"'{text}'"


def file_exists_in_gdrive(service, folder_id, name_fragment):
    """True when the folder already holds a file whose name contains *name_fragment*.

    *name_fragment* MUST be derived from the same string handed to
    :func:`upload_to_gdrive` as the Drive file name — ``<dd-Mon-yyyy>_<prefix>``,
    e.g. ``18-Sep-2026_myanmaalinn``.

    The call site used to pass the *history* key instead
    (``<prefix>_<dd>_<mm>_<yyyy>`` -> ``myanmaalinn_18_09_2026``). Those two
    strings never overlap: the separators differ (``-`` vs ``_``) and the field
    order is reversed, so ``name contains '<file_id>'`` could not match a single
    file this pipeline had ever uploaded. The guard was dead code from the day it
    was written — which is why the Drive folders accumulated duplicate copies of
    the same issue. It matters more than it looks: the CI runner starts from a
    fresh checkout and never restores ``download_history.sqlite3`` (it is
    uploaded as a run artifact, never downloaded), so ``is_uploaded()`` is
    always False there and this was the only duplicate guard left in a run.
    """
    if not service:
        return False

    folder_literal = _drive_query_literal(folder_id)
    name_literal = _drive_query_literal(name_fragment)
    if folder_literal is None or name_literal is None:
        logger.error(
            "Refusing Drive duplicate lookup with an unsafe query value: folder=%r name=%r",
            folder_id,
            name_fragment,
        )
        return False

    try:
        query = (
            f"{folder_literal} in parents and name contains {name_literal} "
            "and trashed = false"
        )
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
    # What the Drive file will actually be called, minus the extension. The
    # duplicate lookup has to search for THIS, not the history key — see
    # file_exists_in_gdrive() for why the two are not interchangeable.
    drive_name_fragment = os.path.splitext(filename)[0]

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

    if service and file_exists_in_gdrive(service, folder_id, drive_name_fragment):
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
                # Record the failure. Previously this branch only logged, so the
                # row stayed absent from download_history: `is_uploaded` returned
                # False forever, the Drive folder silently missed the issue, and
                # the run still reported success to Telegram.
                logger.error("Drive upload failed: %s", drive_err)
                save_history(
                    newspaper=file_prefix, source=source_name, published_date=published_date,
                    source_file_id=file_id, source_url=file_url, filename=filename,
                    status="failed", error=f"Drive upload failed: {drive_err}",
                )
        else:
            save_history(
                newspaper=file_prefix, source=source_name, published_date=published_date,
                source_file_id=file_id, source_url=file_url, filename=filename,
                status="downloaded", drive_url=None,
            )

        # Direct DB Ingestion
        # The PDF is already safely uploaded at this point, so an ingestion
        # failure must not abort the run: without this guard an exception here
        # propagated out of process_newspaper() and the *second* newspaper was
        # never fetched at all.
        try:
            ingest_ok = process_and_ingest_pdf(local_path, name, published_date)
        except Exception as ingest_err:
            logger.exception("Supabase ingestion raised for %s", filename)
            ingest_ok = False
            ingest_error = ingest_err
        else:
            ingest_error = None

        if ingest_ok:
            logger.info("Supabase direct ingestion success for %s", filename)
        else:
            detail = f": {ingest_error}" if ingest_error else ""
            logger.warning("Supabase direct ingestion skipped or failed for %s%s", filename, detail)

        # NOTE: Telegram inline buttons require a real http(s) URL. Passing the
        # literal "Direct DB Ingested" made sendMessage fail with HTTP 400,
        # so the whole notification was silently lost.
        return [(filename, drive_link)], None
    finally:
        if os.path.exists(local_path):
            try: os.remove(local_path)
            except Exception: pass


def notify_run_result(date_label, uploads, failures, drive_urls):
    """Send the run summary and return the message that was sent.

    The dispatch order is the whole point of this function. `uploads` used to be
    tested first, with the failure branch reachable only when nothing had
    succeeded at all — so a run where မြန်မာ့အလင်း uploaded and ကြေးမုံ failed sent
    "✅ လုပ်ငန်းစဉ် အောင်မြင်စွာ ပြီးဆုံးပါပြီ" and never named the missing paper.
    A partial failure now travels with the success message instead.

    Extracted from the ``__main__`` block so the choice of message is reachable
    from tests: code under ``if __name__ == "__main__":`` cannot be exercised by
    importing the module, which is why this ordering was never covered.
    """
    if uploads:
        message, buttons = build_success_notification(
            date_label, uploads, drive_urls, failures
        )
    elif failures:
        message, buttons = build_error_notification(date_label, failures)
    else:
        message, buttons = build_idle_notification(date_label)
    send_telegram_message(message, buttons)
    return message


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD format")
    args, _ = parser.parse_known_args()

    requested_date_str = args.date or os.environ.get("DOWNLOAD_DATE")
    download_date = resolve_download_date(requested_date_str)

    init_database(DATABASE_PATH)
    # Must go through the setter: a bare assignment here would create a
    # __main__-local and leave main.DB_CONNECTION as None (see the docstring on
    # set_database_connection for what that silently broke).
    set_database_connection(sqlite3.connect(DATABASE_PATH))
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
    close_database_connection()
    today_date = download_date.strftime("%d-%b-%Y")
    logger.info("Completed uploads=%d manifest=%s", len(all_uploads), MANIFEST_PATH)

    drive_urls = {
        "မြန်မာ့အလင်း": f"https://drive.google.com/drive/folders/{MAL_FOLDER_ID}",
        "ကြေးမုံ": f"https://drive.google.com/drive/folders/{KM_FOLDER_ID}",
    }
    failures = []
    if err_mal:
        failures.append(("မြန်မာ့အလင်း", err_mal))
    if err_km:
        failures.append(("ကြေးမုံ", err_km))

    notify_run_result(today_date, all_uploads, failures, drive_urls)
    logger.info("Process completed")