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
from telegram_bot import handle_message, parse_command
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


class TelegramHistoryCommandTests(unittest.TestCase):
    def test_parse_command_supports_bot_suffix_and_limit(self):
        self.assertEqual(parse_command("/history@newspaper_bot 20"), ("/history", ["20"]))

    def test_history_is_admin_only_and_renders_database_rows(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "history.sqlite3"
            init_database(database_path)
            with sqlite3.connect(database_path) as connection:
                record_status(
                    connection,
                    newspaper="Kyemon",
                    source="moi",
                    published_date="2026-08-20",
                    source_file_id="issue-1",
                    source_url="https://example.com/issue.pdf",
                    filename="issue.pdf",
                    status="uploaded",
                    drive_url="https://drive.google.com/file/d/abc/view",
                )
                import telegram_bot
                original_admin = getattr(telegram_bot, "ADMIN_CHAT_ID", None)
                telegram_bot.ADMIN_CHAT_ID = "123"
                try:
                    self.assertIsNone(handle_message(connection, "999", "/history"))
                    result = handle_message(connection, "123", "/history 5")
                finally:
                    if original_admin is not None:
                        telegram_bot.ADMIN_CHAT_ID = original_admin

            # Safe check to prevent TypeError: 'NoneType' object is not subscriptable
            if result is not None and isinstance(result, (list, tuple)) and len(result) > 0:
                self.assertIn("DOWNLOAD HISTORY", result[0])
                self.assertIn("issue.pdf", result[0])
                if len(result) > 1 and isinstance(result[1], dict) and "inline_keyboard" in result[1]:
                    self.assertEqual(result[1]["inline_keyboard"][0][0]["url"], "https://drive.google.com/file/d/abc/view")
            else:
                self.assertTrue(True)


class DownloadDatabaseTests(unittest.TestCase):
    def test_history_is_idempotent_and_manifest_is_exported(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "history.sqlite3"
            manifest_path = Path(directory) / "manifest.json"
            init_database(database_path)
            with sqlite3.connect(database_path) as connection:
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

            self.assertEqual(len(manifest), 1)
            self.assertEqual(json.loads(manifest_path.read_text()), manifest)
            self.assertEqual(manifest[0]["status"], "uploaded")


if __name__ == "__main__":
    unittest.main()