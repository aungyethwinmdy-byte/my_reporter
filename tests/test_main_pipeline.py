"""Tests for main.py's per-newspaper pipeline error handling.

`process_newspaper` orchestrates discover → download → Drive upload → ingest for
ONE newspaper. It had no coverage, which is how two silent-failure bugs hid there:

  1. a Drive upload failure was logged but never recorded, so the issue could
     never be retried and the run still reported success;
  2. an ingestion exception propagated out and aborted the run, so the second
     newspaper was never even attempted.

Everything here is offline: network, Drive, SQLite and ingest are all patched.
"""

import re
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import main


def _download_date():
    return date(2026, 9, 18)


class _RecordingDrive:
    """Minimal Drive stub that records the query AND evaluates it.

    A stub that always answered "found" would hide the defect this class exists
    to catch: the *query string* is what was wrong, and a MagicMock (or a fixed
    response) accepts any argument at all. So `list()` records the `q` and
    `execute()` applies Drive's `name contains '<value>'` semantics for real —
    which is what makes the skip test below a genuine regression test rather
    than a restatement of the stub.
    """

    _CONTAINS = re.compile(r"name contains '([^']*)'")

    def __init__(self, existing=()):
        self.queries = []
        self._existing = list(existing)
        self._last_query = None

    def files(self):
        return self

    def list(self, q=None, fields=None):
        self.queries.append(q)
        self._last_query = q
        return self

    def execute(self):
        match = self._CONTAINS.search(self._last_query or "")
        if not match:
            return {"files": []}
        fragment = match.group(1)
        return {"files": [f for f in self._existing if fragment in f["name"]]}


class ProcessNewspaperHarness(unittest.TestCase):
    """Builds a real SQLite history DB plus a temp PDF so cleanup paths run."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.db_path = self.tmp / "history.sqlite3"
        self.manifest_path = self.tmp / "manifest.json"
        main.init_database(self.db_path)
        self.connection = sqlite3.connect(self.db_path)
        self.addCleanup(self.connection.close)

        # Point the module-level helpers at our scratch DB / temp files.
        self._patches = [
            patch.object(main, "DB_CONNECTION", self.connection),
            patch.object(main, "MANIFEST_PATH", str(self.manifest_path)),
            patch.object(main, "find_moi_paper_universal", return_value="https://x/issue.pdf"),
            patch.object(main, "is_uploaded", return_value=False),
            patch.object(main, "download_pdf_to_disk", return_value=True),
            patch.object(main.tempfile, "gettempdir", return_value=str(self.tmp)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _rows(self):
        return self.connection.execute(
            "SELECT status, error, drive_url FROM download_history"
        ).fetchall()


class DriveFailureTests(ProcessNewspaperHarness):
    def test_drive_upload_failure_is_recorded_as_failed(self):
        service = MagicMock()
        with patch.object(main, "file_exists_in_gdrive", return_value=False), patch.object(
            main, "upload_to_gdrive", side_effect=RuntimeError("quota exceeded")
        ), patch.object(main, "process_and_ingest_pdf", return_value=True):
            uploads, error = main.process_newspaper(
                service, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )

        rows = self._rows()
        self.assertEqual(len(rows), 1, "a Drive failure must still write a history row")
        status, err, drive_url = rows[0]
        self.assertEqual(status, "failed")
        self.assertIn("quota exceeded", err)
        self.assertIsNone(drive_url)
        # The file itself was fine, so it is still reported as an upload.
        self.assertEqual(len(uploads), 1)
        self.assertIsNone(error)


class IngestFailureTests(ProcessNewspaperHarness):
    def test_ingest_exception_does_not_abort_the_run(self):
        service = MagicMock()
        with patch.object(main, "file_exists_in_gdrive", return_value=False), patch.object(
            main, "upload_to_gdrive", return_value="https://drive.google.com/file/d/x/view"
        ), patch.object(
            main, "process_and_ingest_pdf", side_effect=RuntimeError("supabase down")
        ):
            # Must not raise — the PDF is already uploaded and the run continues.
            uploads, error = main.process_newspaper(
                service, "folder", "ကြေးမုံ", "km", "themirror",
                "https://www.moi.gov.mm", _download_date(),
            )

        self.assertEqual(len(uploads), 1, "upload still succeeded")
        self.assertIsNone(error, "ingest failure is not a download error")
        self.assertEqual(self._rows()[0][0], "uploaded")

    def test_ingest_returning_false_is_a_soft_failure(self):
        service = MagicMock()
        with patch.object(main, "file_exists_in_gdrive", return_value=False), patch.object(
            main, "upload_to_gdrive", return_value="https://drive.google.com/file/d/x/view"
        ), patch.object(main, "process_and_ingest_pdf", return_value=False):
            uploads, error = main.process_newspaper(
                service, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )
        self.assertEqual(len(uploads), 1)
        self.assertIsNone(error)
        self.assertEqual(self._rows()[0][0], "uploaded")


class NoDriveServiceTests(ProcessNewspaperHarness):
    def test_works_without_drive_and_records_downloaded(self):
        with patch.object(main, "file_exists_in_gdrive", return_value=False), patch.object(
            main, "process_and_ingest_pdf", return_value=True
        ):
            uploads, error = main.process_newspaper(
                None, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )
        self.assertEqual(len(uploads), 1)
        self.assertIsNone(uploads[0][1], "no Drive link without a service")
        self.assertIsNone(error)
        self.assertEqual(self._rows()[0][0], "downloaded")

    def test_temp_pdf_is_cleaned_up(self):
        leftover = self.tmp / "18-Sep-2026_myanmaalinn.pdf"
        leftover.write_bytes(b"%PDF-1.7 test")
        with patch.object(main, "file_exists_in_gdrive", return_value=False), patch.object(
            main, "process_and_ingest_pdf", return_value=True
        ):
            main.process_newspaper(
                None, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )
        self.assertFalse(leftover.exists(), "temp PDF must be removed even when ingest fails")


class DownloadFailureTests(ProcessNewspaperHarness):
    def test_download_failure_records_failed_and_returns_error(self):
        with patch.object(main, "download_pdf_to_disk", return_value=False):
            uploads, error = main.process_newspaper(
                None, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )
        self.assertEqual(uploads, [])
        self.assertIsNotNone(error)
        self.assertEqual(self._rows()[0][0], "failed")

    def test_missing_source_returns_error_without_history_row(self):
        with patch.object(main, "find_moi_paper_universal", return_value=None), patch.object(
            main, "get_mdn_backup_papers", return_value=[]
        ):
            uploads, error = main.process_newspaper(
                None, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )
        self.assertEqual(uploads, [])
        self.assertIsNotNone(error)
        self.assertEqual(self._rows(), [], "nothing discovered -> nothing to record")


class DatabaseConnectionBindingTests(unittest.TestCase):
    """The history connection must actually reach the module global.

    Every test above patches ``main.DB_CONNECTION`` directly, which is exactly
    why the real defect went unnoticed: ``__main__`` did a bare
    ``DB_CONNECTION = sqlite3.connect(...)``, and a bare assignment inside the
    ``if __name__ == "__main__":`` block binds a *__main__-local*, leaving
    ``main.DB_CONNECTION`` as ``None``. ``save_history()`` then hit its early
    ``if DB_CONNECTION is None: return`` on every call:

      * no history row was ever written, so ``is_uploaded()`` always returned
        False and the same issue was re-downloaded and re-uploaded every day;
      * ``export_manifest()`` wrote an empty manifest;
      * the Drive-failure and ingest-failure fixes above could never persist.

    These tests go through the public setter instead of patching, so a
    regression to a bare assignment is caught.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "history.sqlite3"
        main.init_database(self.db_path)
        # Always leave the module global pointing somewhere harmless.
        self.addCleanup(main.close_database_connection)

    def test_setter_binds_the_module_global(self):
        self.assertIsNone(main.DB_CONNECTION, "precondition: starts unbound")
        connection = main.set_database_connection(sqlite3.connect(self.db_path))
        self.assertIsNotNone(main.DB_CONNECTION, "module global must be bound")
        self.assertIs(main.DB_CONNECTION, connection)
        # close_database_connection (registered in setUp) releases the handle.

    def test_save_history_persists_through_the_module_global(self):
        connection = main.set_database_connection(sqlite3.connect(self.db_path))
        main.save_history(
            newspaper="mal", source="moi", published_date="2026-09-18",
            source_file_id="mal_18_09_2026", source_url="https://x/issue.pdf",
            filename="18-Sep-2026_myanmaalinn.pdf", status="uploaded",
        )
        # Read back through the SAME connection. Opening a second one and
        # leaving it open would keep the file locked and break the
        # TemporaryDirectory cleanup on Windows (WinError 32) — the very
        # leak this codebase already had to fix once in init_database().
        rows = connection.execute("SELECT status FROM download_history").fetchall()
        self.assertEqual(rows, [("uploaded",)], "history row must be persisted")

    def test_save_history_without_a_connection_is_a_silent_noop(self):
        """The guard is intentional — it must not raise, just do nothing."""
        self.assertIsNone(main.DB_CONNECTION)
        main.save_history(
            newspaper="mal", source="moi", published_date="2026-09-18",
            source_file_id="x", source_url="u", filename="f.pdf", status="uploaded",
        )

    def test_close_clears_the_global(self):
        main.set_database_connection(sqlite3.connect(self.db_path))
        main.close_database_connection()
        self.assertIsNone(main.DB_CONNECTION, "must not leave a dangling handle")
        # Idempotent: a second close is harmless.
        main.close_database_connection()

    def test_is_uploaded_sees_rows_written_through_the_setter(self):
        """End-to-end: the duplicate check that guards re-downloading."""
        main.set_database_connection(sqlite3.connect(self.db_path))
        file_id = "mal_18_09_2026"
        self.assertFalse(main.is_uploaded(main.DB_CONNECTION, "mal", file_id))
        main.save_history(
            newspaper="mal", source="moi", published_date="2026-09-18",
            source_file_id=file_id, source_url="https://x/issue.pdf",
            filename="18-Sep-2026_myanmaalinn.pdf", status="uploaded",
        )
        self.assertTrue(
            main.is_uploaded(main.DB_CONNECTION, "mal", file_id),
            "a recorded upload must suppress the next day's re-download",
        )

    def test_main_block_does_not_assign_db_connection_directly(self):
        """Static guard: the __main__ block must not reintroduce the bare bind.

        A top-level ``DB_CONNECTION = ...`` outside a function shadows the
        module global for the rest of ``__main__`` only, which is invisible to
        runtime tests that patch the attribute.
        """
        import ast
        import inspect

        source = inspect.getsource(main)
        tree = ast.parse(source)
        offenders = []
        for node in tree.body:
            # Only inspect the `if __name__ == "__main__":` block.
            if not isinstance(node, ast.If):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "DB_CONNECTION":
                            offenders.append(child.lineno)
        self.assertEqual(
            offenders,
            [],
            f"bare DB_CONNECTION assignment in __main__ at line(s) {offenders}; "
            "use set_database_connection() instead",
        )


class DriveDuplicateLookupTests(ProcessNewspaperHarness):
    """The Drive duplicate guard must search for a name the pipeline uploads.

    ``file_exists_in_gdrive`` was called with ``file_id``
    (``myanmaalinn_18_09_2026``) while the Drive file is named
    ``18-Sep-2026_myanmaalinn.pdf`` — different separators (``-`` vs ``_``) and
    reversed field order — so ``name contains`` could never match and the guard
    was dead code from the start. Every pre-existing test patched the helper
    out, which is exactly why the wrong argument went unnoticed: a MagicMock
    accepts any argument. These tests drive the real call site and inspect the
    query string that reaches Drive.
    """

    def _run(self, drive):
        with patch.object(
            main, "upload_to_gdrive",
            return_value="https://drive.google.com/file/d/x/view",
        ):
            return main.process_newspaper(
                drive, "folder", "မြန်မာ့အလင်း", "mal", "myanmaalinn",
                "https://www.moi.gov.mm", _download_date(),
            )

    def test_query_searches_for_the_uploaded_file_name(self):
        drive = _RecordingDrive()
        with patch.object(main, "process_and_ingest_pdf", return_value=True):
            self._run(drive)

        self.assertEqual(len(drive.queries), 1)
        query = drive.queries[0]
        self.assertIn(
            "18-Sep-2026_myanmaalinn", query,
            "the lookup must be able to match the name upload_to_gdrive() sets",
        )
        self.assertNotIn(
            "myanmaalinn_18_09_2026", query,
            "the history key appears in no Drive file name — it can never match",
        )

    def test_existing_drive_copy_is_skipped_without_downloading(self):
        drive = _RecordingDrive(
            existing=[{"id": "x", "name": "18-Sep-2026_myanmaalinn.pdf"}]
        )
        uploads, error = self._run(drive)

        self.assertEqual(uploads, [], "a Drive duplicate must not be re-uploaded")
        self.assertIsNone(error)
        self.assertFalse(
            main.download_pdf_to_disk.called,
            "the duplicate check must run before the download",
        )
        status, err, _ = self._rows()[0]
        self.assertEqual(status, "skipped")
        self.assertIn("Google Drive", err)

    def test_no_match_still_downloads_and_uploads(self):
        drive = _RecordingDrive()
        with patch.object(main, "process_and_ingest_pdf", return_value=True):
            uploads, error = self._run(drive)
        self.assertEqual(len(uploads), 1)
        self.assertIsNone(error)
        self.assertEqual(self._rows()[0][0], "uploaded")


class DriveQueryLiteralTests(unittest.TestCase):
    """Drive's ``q`` has no parameter binding, so unsafe values must be refused."""

    def test_quotes_a_plain_value(self):
        self.assertEqual(
            main._drive_query_literal("mal_18_09_2026"), "'mal_18_09_2026'"
        )

    def test_refuses_values_that_would_break_out_of_the_literal(self):
        for unsafe in ("a'b", "a\\b", "", None):
            with self.subTest(value=unsafe):
                self.assertIsNone(main._drive_query_literal(unsafe))

    def test_lookup_refuses_an_unsafe_name_without_querying_drive(self):
        drive = _RecordingDrive()
        self.assertFalse(
            main.file_exists_in_gdrive(drive, "folder", "18-Sep-2026_' or '1'='1")
        )
        self.assertEqual(drive.queries, [], "no query may be sent for an unsafe value")

    def test_lookup_without_a_service_is_false(self):
        self.assertFalse(main.file_exists_in_gdrive(None, "folder", "anything"))


class RunNotificationTests(unittest.TestCase):
    """A partial failure must not be reported as a clean success.

    The ``__main__`` dispatch tested ``uploads`` first and reached the error
    branch only when nothing had succeeded at all, so a run where
    မြန်မာ့အလင်း uploaded and ကြေးမုံ failed sent the ✅ success message and never
    named the missing paper — the operator had to read the logs to find out.
    """

    def _notify(self, uploads, failures):
        sent = []
        with patch.object(
            main,
            "send_telegram_message",
            side_effect=lambda message, buttons=None: sent.append(message),
        ):
            returned = main.notify_run_result("18-Sep-2026", uploads, failures, {})
        self.assertEqual(len(sent), 1, "exactly one summary per run")
        self.assertEqual(sent[0], returned, "must return what it sent")
        return returned

    def test_partial_failure_is_named_in_the_success_message(self):
        message = self._notify(
            [("18-Sep-2026_myanmaalinn.pdf", "https://drive.google.com/file/d/x/view")],
            [("ကြေးမုံ", "ကြေးမုံ အတွက် PDF ရှာမတွေ့ပါ")],
        )
        self.assertIn("အောင်မြင်စွာ", message, "the good news is still reported")
        self.assertIn("18-Sep-2026_myanmaalinn.pdf", message)
        self.assertIn("ကြေးမုံ", message, "the failed newspaper must be named")
        self.assertIn("PDF ရှာမတွေ့ပါ", message)

    def test_total_failure_uses_the_error_notification(self):
        message = self._notify([], [("မြန်မာ့အလင်း", "boom")])
        self.assertIn("သတိပေးချက်ရှိပါသည်", message)

    def test_no_uploads_and_no_failures_is_idle(self):
        message = self._notify([], [])
        self.assertIn("အသစ်တင်ရန် မရှိသေးပါ", message)


if __name__ == "__main__":
    unittest.main()
