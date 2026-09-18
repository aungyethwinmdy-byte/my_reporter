"""Diagnose duplicate PDFs in the Newsroom Google Drive folders.

WHY THIS EXISTS
---------------
``main.py``'s ``__main__`` block used to do::

    DB_CONNECTION = sqlite3.connect(DATABASE_PATH)

A bare assignment inside ``if __name__ == "__main__":`` binds a *__main__*-local,
so ``main.DB_CONNECTION`` stayed ``None`` for the whole run. ``save_history()``
opens with ``if DB_CONNECTION is None: return``, so every call silently discarded
its row. Two consequences mattered:

1. ``is_uploaded()`` always returned False, so the duplicate check never fired and
   the same issue was downloaded and re-uploaded to Google Drive on every run.
2. ``file_exists_in_gdrive()`` is a *backup* guard, but it only runs when
   ``drive_service`` is truthy AND matches on ``file_id`` (``mal_18_09_2026``)
   appearing in the *name*. Any file uploaded under a slightly different name, or
   uploaded while Drive was unconfigured, is invisible to it.

This script lists what is actually in the folders and groups by the download-date
suffix so duplicates are obvious. It is READ-ONLY: it never deletes or renames.

USAGE
-----
Run from the repo root so ``load_dotenv()`` finds ``.env``::

    python scripts/check_drive_duplicates.py

Requires GDRIVE_REFRESH_TOKEN / GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET.
Folder IDs default to the ones in main.py and can be overridden with
MAL_FOLDER_ID / KM_FOLDER_ID.
"""

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Import from the repo root, not from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import main  # noqa: E402

# Imported lazily-ish: main.py already guards these behind try/except ImportError
# so the test suite can run without the Drive SDK, and this script must not
# crash outright when it is absent either.
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    GDRIVE_SDK_AVAILABLE = True
except ImportError:
    GDRIVE_SDK_AVAILABLE = False

# Files are uploaded as f"{date}_{file_prefix}.pdf", e.g. 18-Sep-2026_myanmaalinn.pdf
ISSUE_RE = re.compile(r"^(?P<date>\d{2}-[A-Za-z]{3}-\d{4})_(?P<paper>[A-Za-z]+)")
# Drive keeps previous revisions; a duplicate name is a separate file object.
PAGE_SIZE = 1000


def build_service():
    if not GDRIVE_SDK_AVAILABLE:
        print("ERROR: Google Drive SDK is not installed in this environment.")
        print("       Install it with:")
        print("         pip install google-api-python-client google-auth")
        return None
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    if not (refresh_token and client_id and client_secret):
        print("ERROR: GDRIVE_REFRESH_TOKEN / GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET are not set.")
        print("       Add them to .env (or export them) and re-run.")
        return None
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def list_folder(service, folder_id, label):
    """Return every non-trashed file directly inside folder_id."""
    files = []
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, size, createdTime, mimeType)",
                pageSize=PAGE_SIZE,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    print(f"\n{'=' * 62}")
    print(f"{label}  (folder {folder_id})")
    print(f"{'=' * 62}")
    print(f"Total files: {len(files)}")
    if not files:
        return files

    # Group by "<issue-date>_<paper>" so a re-upload of the same issue stands out.
    grouped = defaultdict(list)
    unparsed = []
    for f in files:
        stem = f["name"].rsplit(".", 1)[0]
        m = ISSUE_RE.match(stem)
        if m:
            grouped[(m.group("date"), m.group("paper"))].append(f)
        else:
            unparsed.append(f)

    duplicates = {k: v for k, v in grouped.items() if len(v) > 1}

    print(f"Distinct issues: {len(grouped)}")
    if duplicates:
        print(f"\n!! DUPLICATES FOUND: {len(duplicates)} issue(s) uploaded more than once")
        extra = sum(len(v) - 1 for v in duplicates.values())
        print(f"!! Redundant files that should not exist: {extra}")
        print("\n" + "-" * 62)
        # Newest first: the extra copies are the ones worth removing.
        for (issue_date, paper), group in sorted(duplicates.items(), reverse=True):
            group.sort(key=lambda f: f.get("createdTime") or "")
            print(f"\n{issue_date}  {paper}   -> {len(group)} copies")
            for idx, f in enumerate(group):
                marker = "KEEP " if idx == 0 else "EXTRA"
                size = f.get("size", "?")
                created = (f.get("createdTime") or "?")[:19]
                print(f"  [{marker}] {created}  {int(size):>9,} bytes  id={f['id']}")
                print(f"           {f['name']}")
    else:
        print("\nNo duplicates detected by name+date grouping.")

    if unparsed:
        print(f"\n{len(unparsed)} file(s) did not match the date_paper naming pattern:")
        for f in unparsed[:20]:
            print(f"  - {f['name']}  (id={f['id']})")
        if len(unparsed) > 20:
            print(f"  ... and {len(unparsed) - 20} more")

    return files


def main_entry():
    service = build_service()
    if service is None:
        return 1

    mal_id = os.environ.get("MAL_FOLDER_ID") or main.MAL_FOLDER_ID
    km_id = os.environ.get("KM_FOLDER_ID") or main.KM_FOLDER_ID

    try:
        mal_files = list_folder(service, mal_id, "မြန်မာ့အလင်း (Myanma Alinn)")
        km_files = list_folder(service, km_id, "ကြေးမုံ (Kyemon)")
    except Exception as error:
        print(f"ERROR talking to Drive: {error}")
        print("If this is a 403, the refresh token likely lacks drive.readonly.")
        return 1

    print(f"\n{'=' * 62}")
    print("SUMMARY")
    print(f"{'=' * 62}")
    print(f"  မြန်မာ့အလင်း: {len(mal_files)} file(s)")
    print(f"  ကြေးမုံ     : {len(km_files)} file(s)")
    print("\nThis script is read-only. Nothing was deleted or modified.")
    print("Review the EXTRA entries above, then remove them in the Drive UI.")
    print("\nNote: TLS verification is ON. To bypass a broken proxy locally, set")
    print("      NEWSROOM_INSECURE_TLS=1 (not recommended).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
