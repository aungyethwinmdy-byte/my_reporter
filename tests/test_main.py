import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from database import export_manifest, init_database, is_uploaded, record_status
from notifications import (
    build_error_notification,
    build_history_notification,
    build_idle_notification,
    build_success_notification,
)
from utils import absolute_url, extract_pdf_links, is_valid_pdf


class DownloaderHelperTests(unittest.TestCase):
    def test_extract_pdf_links_supports_pdf_and_download_paths(self):
        html = Path("fixtures/moi_sample.html").read_text(encoding="utf-8")
        self.assertEqual(
            extract_pdf_links(html),
            [
                "/file-download/download/public/myanma-alinn-2026-08-20",
                "/archive/older-issue.pdf",
            ],
        )

    def test_mdn_fixture_contains_pdf_link(self):
        html = Path("fixtures/mdn_sample.html").read_text(encoding="utf-8")
        self.assertEqual(
            extract_pdf_links(html),
            ["/sites/default/files/mal-newspaper-2026-08-20.pdf"],
        )

    def test_is_valid_pdf_checks_signature(self):
        self.assertTrue(is_valid_pdf(b"%PDF-1.7\ncontent"))
        self.assertFalse(is_valid_pdf(b"<html>error</html>"))
        self.assertFalse(is_valid_pdf(b""))

    def test_absolute_url_handles_relative_and_absolute_urls(self):
        self.assertEqual(
            absolute_url("/files/today.pdf", "https://example.com/"),
            "https://example.com/files/today.pdf",
        )
        self.assertEqual(
            absolute_url("https://cdn.example.com/today.pdf", "https://example.com"),
            "https://cdn.example.com/today.pdf",
        )


class NotificationTests(unittest.TestCase):
    def test_success_notification_contains_card_content_and_buttons(self):
        message, markup = build_success_notification(
            "20-Aug-2026",
            [("issue.pdf", "https://drive.google.com/file/d/abc/view")],
            {"မြန်မာ့အလင်း": "https://drive.google.com/drive/folders/mal"},
        )
        self.assertIn("လုပ်ငန်းစဉ် အောင်မြင်စွာ ပြီးဆုံးပါပြီ", message)
        self.assertIn("issue.pdf", message)
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://drive.google.com/file/d/abc/view")
        self.assertEqual(markup["inline_keyboard"][1][0]["url"], "https://drive.google.com/drive/folders/mal")

    def test_error_and_idle_notifications_have_expected_status(self):
        error_message, error_markup = build_error_notification(
            "20-Aug-2026", [("Kyemon", "source unavailable")]
        )
        idle_message, idle_markup = build_idle_notification("20-Aug-2026")
        self.assertIn("သတိပေးချက်ရှိပါသည်", error_message)
        self.assertIn("source unavailable", error_message)
        self.assertIsNone(error_markup)
        self.assertIn("အသစ်တင်ရန် မရှိသေးပါ", idle_message)
        self.assertIsNone(idle_markup)

    def test_buttons_skip_non_url_links(self):
        # Drive မသုံးပါက link သည် None ဖြစ်ပြီး Telegram button မဖန်တီးရပါ။
        message, markup = build_success_notification(
            "20-Aug-2026",
            [("issue.pdf", None)],
            {"မြန်မာ့အလင်း": "https://drive.google.com/drive/folders/mal"},
        )
        self.assertIsNotNone(markup)
        urls = [btn["url"] for row in markup["inline_keyboard"] for btn in row]
        self.assertTrue(all(u.startswith("https://") for u in urls))
        self.assertEqual(len(markup["inline_keyboard"]), 1)

    def test_history_notification_renders_rows_and_counts(self):
        rows = [
            {
                "newspaper": "Kyemon",
                "filename": "issue.pdf",
                "published_date": "2026-08-20",
                "status": "uploaded",
                "error": None,
                "drive_url": "https://drive.google.com/file/d/abc/view",
            }
        ]
        counts = {"total": 1, "uploaded": 1, "skipped": 0, "failed": 0}
        message, markup = build_history_notification(rows, counts, 5)
        self.assertIn("DOWNLOAD HISTORY", message)
        self.assertIn("issue.pdf", message)
        self.assertEqual(
            markup["inline_keyboard"][0][0]["url"],
            "https://drive.google.com/file/d/abc/view",
        )


class DownloadDatabaseTests(unittest.TestCase):
    def test_history_is_idempotent_and_manifest_is_exported(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "history.sqlite3"
            manifest_path = Path(directory) / "manifest.json"
            init_database(database_path)
            # sqlite3.connect() must be closed explicitly: `contextlib.closing`
            # only ends the transaction, the file handle outlives the `with`
            # block and Windows then refuses to remove the temp directory
            # (WinError 32) inside TemporaryDirectory.__exit__.
            connection = sqlite3.connect(database_path)
            try:
                record_status(
                    connection,
                    newspaper="Myanma_Alinn",
                    source="moi",
                    published_date="2026-08-20",
                    source_file_id="issue-1",
                    source_url="https://example.com/issue-1.pdf",
                    filename="issue-1.pdf",
                    status="uploaded",
                    drive_url="https://drive.google.com/file/d/abc/view",
                )
                self.assertTrue(is_uploaded(connection, "Myanma_Alinn", "issue-1"))
                record_status(
                    connection,
                    newspaper="Myanma_Alinn",
                    source="moi",
                    published_date="2026-08-20",
                    source_file_id="issue-1",
                    source_url="https://example.com/issue-1.pdf",
                    filename="issue-1.pdf",
                    status="uploaded",
                    drive_url="https://drive.google.com/file/d/abc/view",
                )
                manifest = export_manifest(connection, manifest_path)
            finally:
                connection.close()

            self.assertEqual(len(manifest), 1)
            self.assertEqual(json.loads(manifest_path.read_text()), manifest)
            self.assertEqual(manifest[0]["status"], "uploaded")

    def test_duplicate_rows_are_updated_not_inserted(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "history.sqlite3"
            init_database(database_path)
            connection = sqlite3.connect(database_path)
            try:
                for status in ("downloaded", "uploaded"):
                    record_status(
                        connection,
                        newspaper="themirror",
                        source="mdn",
                        published_date="2026-08-20",
                        source_file_id="km-1",
                        source_url="https://example.com/km.pdf",
                        filename="km.pdf",
                        status=status,
                        drive_url=None if status == "downloaded" else "https://drive.google.com/x",
                    )
                row = connection.execute(
                    "SELECT status, drive_url FROM download_history WHERE source_file_id = 'km-1'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row[0], "uploaded")
            self.assertEqual(row[1], "https://drive.google.com/x")


class ModuleImportTests(unittest.TestCase):
    """Missing credentials / optional libs ကြောင့် import မပျက်စေရန် အာမခံချက်။"""

    CORE = ("database", "notifications", "utils", "ingest_engine", "main")
    BOT = (
        "telegram_bot",
        "cross_source_verifier",
        "fetch_independent_news",
        "auto_numeric_extractor",
        "numeric_intelligence_engine",
        "newsroom_mcp",
        "report_formatter",
        "system_router",
    )

    def test_core_modules_import_cleanly(self):
        for module_name in self.CORE:
            with self.subTest(module=module_name):
                __import__(module_name)

    def test_newsroom_bot_modules_import_cleanly(self):
        for module_name in self.BOT:
            with self.subTest(module=module_name):
                __import__(module_name)


if __name__ == "__main__":
    unittest.main()
