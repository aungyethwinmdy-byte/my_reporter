"""Guards for the newspaper scraper's performance behaviour.

`_fetch_html` is monkeypatched, so these tests never touch the network.
They lock in two things that previously cost the workflow its 15-minute
job budget:
  1. candidate URLs are probed in parallel, not one after another;
  2. the whole MOI phase is capped by MOI_PHASE_BUDGET.
"""

import os
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import utils
from utils import download_pdf_to_disk, find_moi_paper_universal, get_mdn_backup_papers


class FakeFetcher:
    """Replaces utils._fetch_html. Sleeps, then serves canned HTML."""

    def __init__(self, good_html=None, delay=0.4, good_pred=lambda url: False):
        self.good_html = good_html or (
            '<a href="/file-download/download/public/mal-2026-09-15">issue</a>'
        )
        self.delay = delay
        self.good_pred = good_pred
        self.calls = 0

    def __call__(self, url, timeout, params=None):
        self.calls += 1
        time.sleep(self.delay)
        if params is not None:  # MDN probe
            return None
        return self.good_html if self.good_pred(url) else None


class TlsVerificationTests(unittest.TestCase):
    """TLS verification must stay ON unless explicitly opted out.

    The scraper used to call verify=False on every request, silently accepting
    forged certificates for the very PDFs that get ingested into the newsroom DB.
    """

    def test_verification_is_enabled_by_default(self):
        # Import in a clean env: NEWSROOM_INSECURE_TLS must be unset here.
        import os

        if "NEWSROOM_INSECURE_TLS" in os.environ:
            self.skipTest("NEWSROOM_INSECURE_TLS set in this environment")
        self.assertTrue(
            utils.VERIFY_TLS,
            "TLS verification must default to ON (opt out via NEWSROOM_INSECURE_TLS=1)",
        )

    def test_insecure_flag_parsing(self):
        """Parsing now lives in env_config.get_bool (shared, never raises)."""
        from env_config import get_bool

        for raw, expected in (
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("", False),
            ("   ", False),
            ("nonsense", False),
        ):
            self.assertEqual(
                get_bool("NEWSROOM_INSECURE_TLS", False, environ={"NEWSROOM_INSECURE_TLS": raw}),
                expected,
                f"NEWSROOM_INSECURE_TLS={raw!r}",
            )

    def test_no_hardcoded_verify_false_in_source(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"verify\s*=\s*False", line):
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [], f"TLS verification disabled at: {offenders}")


class ProbeIsolationTests(unittest.TestCase):
    """One failing probe must not discard another probe's valid result.

    Regression: `_probe_for_pdf_links` wrapped `future.result()` in a blanket
    `except Exception`, so a single transient network error aborted the search
    and returned None even when a sibling probe had already found the PDF.
    """

    GOOD_HTML = '<a href="/file-download/download/public/mal-2026-09-15">issue</a>'
    EXPECTED = "https://www.moi.gov.mm/file-download/download/public/mal-2026-09-15"

    def setUp(self):
        self._orig = utils._fetch_html

    def tearDown(self):
        utils._fetch_html = self._orig

    def test_failing_probe_does_not_discard_successful_one(self):
        def fetcher(url, timeout, params=None):
            if "broken" in url:
                raise RuntimeError("transient network error")
            if "good" in url:
                return self.GOOD_HTML
            return None

        utils._fetch_html = fetcher
        result = utils._probe_for_pdf_links(
            ["https://www.moi.gov.mm/broken", "https://www.moi.gov.mm/good"],
            "https://www.moi.gov.mm",
            timeout=5,
            deadline=time.monotonic() + 10,
        )
        self.assertEqual(
            result,
            self.EXPECTED,
            "a single dead probe must not abort the search",
        )

    def test_all_probes_failing_returns_none_without_raising(self):
        def fetcher(url, timeout, params=None):
            raise RuntimeError("everything is down")

        utils._fetch_html = fetcher
        result = utils._probe_for_pdf_links(
            ["https://www.moi.gov.mm/a", "https://www.moi.gov.mm/b"],
            "https://www.moi.gov.mm",
            timeout=5,
            deadline=time.monotonic() + 10,
        )
        self.assertIsNone(result)

    def test_mdn_probe_isolation(self):
        def fetcher(url, timeout, params=None):
            fmt = params.get("published_date") if params else None
            if fmt == "15/09/2026":
                raise RuntimeError("one format blew up")
            if fmt == "15-09-2026":
                return '<a href="/newspaper/public/ebooks/download/999">A</a>'
            return None

        utils._fetch_html = fetcher
        papers = utils.get_mdn_backup_papers("15", "09", "2026")
        self.assertEqual(
            papers,
            [
                {
                    "name": "မြန်မာ့အလင်း",
                    "file_prefix": "myanmaalinn",
                    "url": "https://www.mdn.gov.mm/newspaper/public/ebooks/download/999",
                }
            ],
            "a failing date-format probe must not hide another format's hit",
        )


class FindMoiPaperTests(unittest.TestCase):
    def setUp(self):
        self._orig = utils._fetch_html
        self._budget = utils.MOI_PHASE_BUDGET

    def tearDown(self):
        utils._fetch_html = self._orig
        utils.MOI_PHASE_BUDGET = self._budget

    def test_returns_pdf_link_when_a_slug_matches(self):
        fetcher = FakeFetcher(good_pred=lambda url: url.endswith("15-sep-2026"))
        utils._fetch_html = fetcher

        result = find_moi_paper_universal(
            "mal", "https://www.moi.gov.mm", "15", "09", "2026"
        )
        self.assertEqual(
            result,
            "https://www.moi.gov.mm/file-download/download/public/mal-2026-09-15",
        )

    def test_candidates_are_probed_in_parallel(self):
        # 9 candidate slugs x 0.4s each would be ~3.6s sequentially.
        fetcher = FakeFetcher(delay=0.4, good_pred=lambda url: False)
        utils._fetch_html = fetcher

        started = time.monotonic()
        find_moi_paper_universal("mal", "https://www.moi.gov.mm", "15", "09", "2026")
        elapsed = time.monotonic() - started

        self.assertGreater(fetcher.calls, 1)
        self.assertLess(elapsed, 3.0)

    def test_phase_budget_caps_total_time(self):
        fetcher = FakeFetcher(delay=0.5, good_pred=lambda url: False)
        utils._fetch_html = fetcher
        utils.MOI_PHASE_BUDGET = 0.5

        started = time.monotonic()
        result = find_moi_paper_universal(
            "mal", "https://www.moi.gov.mm", "15", "09", "2026"
        )
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(elapsed, 5.0)

    def test_returns_none_when_nothing_matches(self):
        utils._fetch_html = FakeFetcher(good_pred=lambda url: False)
        self.assertIsNone(
            find_moi_paper_universal("km", "https://www.moi.gov.mm", "15", "09", "2026")
        )


class MdnBackupTests(unittest.TestCase):
    def setUp(self):
        self._orig = utils._fetch_html

    def tearDown(self):
        utils._fetch_html = self._orig

    def test_parses_ebook_ids_into_paper_records(self):
        html = (
            '<a href="/newspaper/public/ebooks/download/111">A</a>'
            '<a href="/newspaper/public/ebooks/read/222">B</a>'
        )

        def fake(url, timeout, params=None):
            return html if params and params.get("published_date") == "15/09/2026" else None

        utils._fetch_html = fake
        papers = get_mdn_backup_papers("15", "09", "2026")

        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0]["file_prefix"], "myanmaalinn")
        self.assertEqual(
            papers[0]["url"],
            "https://www.mdn.gov.mm/newspaper/public/ebooks/download/111",
        )

    def test_returns_empty_list_when_no_ids_found(self):
        utils._fetch_html = lambda url, timeout, params=None: "<html>no ids</html>"
        self.assertEqual(get_mdn_backup_papers("15", "09", "2026"), [])


class _FakeStreamResponse:
    """Minimal stand-in for `requests.get(..., stream=True)`."""

    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class DownloadVerificationTests(unittest.TestCase):
    """`download_pdf_to_disk` must reject anything that is not really a PDF.

    The old implementation accepted on size alone (>5 KB). Both government
    portals serve an HTML maintenance/login page with HTTP 200 when they are
    down, and that page is bigger than the threshold — so it was saved as
    ".pdf", uploaded to Drive as application/pdf, and recorded as
    status="uploaded". `is_uploaded()` then skipped the real issue for the rest
    of the day and the junk file stayed in the shared folder.
    """

    def setUp(self):
        self._real_get = utils.requests.get
        self.addCleanup(lambda: setattr(utils.requests, "get", self._real_get))
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "issue.pdf")

    def _serve(self, body):
        utils.requests.get = lambda *a, **k: _FakeStreamResponse(body)

    def test_html_maintenance_page_is_rejected(self):
        # HTTP 200, larger than the 5 KB threshold, but not a PDF.
        self._serve(b"<html><body>" + b"Server maintenance. " * 400 + b"</body></html>")
        self.assertFalse(download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path))

    def test_rejected_response_leaves_no_junk_file_behind(self):
        self._serve(b"<html><body>" + b"Server maintenance. " * 400 + b"</body></html>")
        download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path)
        self.assertFalse(
            os.path.exists(self.path),
            "a non-PDF response must not be left on disk for upload",
        )

    def test_login_redirect_body_is_rejected(self):
        self._serve(b'{"error":"unauthorized","detail":"' + b"x" * 6000 + b'"}')
        self.assertFalse(download_pdf_to_disk("https://www.mdn.gov.mm/y.pdf", self.path))

    def test_real_pdf_is_accepted(self):
        self._serve(b"%PDF-1.7\n" + b"0" * 8000 + b"\n%%EOF")
        self.assertTrue(download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path))
        self.assertTrue(os.path.exists(self.path))

    def test_leading_bom_and_newline_are_tolerated(self):
        # Some servers emit a BOM or a stray newline before the header.
        self._serve(b"\xef\xbb\xbf\n%PDF-1.7\n" + b"0" * 8000)
        self.assertTrue(download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path))

    def test_truncated_pdf_is_rejected(self):
        self._serve(b"%PDF-1.7\nshort")
        self.assertFalse(download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path))

    def test_network_error_returns_false_and_cleans_up(self):
        def boom(*a, **k):
            raise ConnectionError("connection reset")

        utils.requests.get = boom
        self.assertFalse(download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_is_valid_pdf_is_actually_used_by_the_downloader(self):
        """Guards the wiring, not just the helper.

        `is_valid_pdf` existed and was unit-tested but nothing called it, which
        is precisely how the size-only check shipped. Patch it out and the
        downloader must change behaviour.
        """
        self._serve(b"%PDF-1.7\n" + b"0" * 8000)
        with patch.object(utils, "is_valid_pdf", return_value=False):
            self.assertFalse(
                download_pdf_to_disk("https://www.moi.gov.mm/x.pdf", self.path)
            )


if __name__ == "__main__":
    unittest.main()
