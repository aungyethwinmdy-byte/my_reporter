import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    newspaper TEXT NOT NULL,
    source TEXT NOT NULL,
    published_date TEXT,
    source_file_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    drive_file_id TEXT,
    drive_url TEXT,
    status TEXT NOT NULL,
    error TEXT,
    discovered_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(newspaper, source_file_id)
);
CREATE INDEX IF NOT EXISTS idx_download_history_status
    ON download_history(status);
CREATE INDEX IF NOT EXISTS idx_download_history_published_date
    ON download_history(published_date);
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def init_database(path):
    db_path = Path(path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(SCHEMA)
    return str(db_path)


def is_uploaded(connection, newspaper, source_file_id):
    row = connection.execute(
        """
        SELECT 1 FROM download_history
        WHERE newspaper = ? AND source_file_id = ? AND status = 'uploaded'
        LIMIT 1
        """,
        (newspaper, source_file_id),
    ).fetchone()
    return row is not None


def record_status(
    connection,
    *,
    newspaper,
    source,
    published_date,
    source_file_id,
    source_url,
    filename,
    status,
    drive_file_id=None,
    drive_url=None,
    error=None,
):
    now = utc_now()
    completed_at = now if status in {"uploaded", "failed", "skipped"} else None
    connection.execute(
        """
        INSERT INTO download_history (
            newspaper, source, published_date, source_file_id, source_url,
            filename, drive_file_id, drive_url, status, error,
            discovered_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(newspaper, source_file_id) DO UPDATE SET
            source = excluded.source,
            published_date = excluded.published_date,
            source_url = excluded.source_url,
            filename = excluded.filename,
            drive_file_id = excluded.drive_file_id,
            drive_url = excluded.drive_url,
            status = excluded.status,
            error = excluded.error,
            completed_at = excluded.completed_at
        """,
        (
            newspaper,
            source,
            published_date,
            source_file_id,
            source_url,
            filename,
            drive_file_id,
            drive_url,
            status,
            error,
            now,
            completed_at,
        ),
    )
    connection.commit()


def fetch_history(connection, limit=10):
    limit = max(1, min(int(limit), 50))
    cursor = connection.execute(
        """
        SELECT newspaper, source, published_date, filename, drive_url,
               status, error, completed_at
        FROM download_history
        ORDER BY COALESCE(completed_at, discovered_at) DESC
        LIMIT ?
        """,
        (limit,),
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def history_counts(connection):
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM download_history
        GROUP BY status
        """
    ).fetchall()
    counts = {"uploaded": 0, "skipped": 0, "failed": 0}
    counts.update({status: count for status, count in rows})
    counts["total"] = sum(counts.values())
    return counts


def export_manifest(connection, output_path):
    cursor = connection.execute(
        """
        SELECT newspaper, source, published_date, source_file_id, source_url,
               filename, drive_file_id, drive_url, status, error,
               discovered_at, completed_at
        FROM download_history
        ORDER BY discovered_at DESC
        """
    )
    rows = cursor.fetchall()
    columns = [column[0] for column in cursor.description]
    manifest = [dict(zip(columns, row)) for row in rows]
    Path(output_path).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
