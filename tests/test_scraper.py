"""Guards for the newspaper scraper's performance behaviour.

`_fetch_html` is monkeypatched, so these tests never touch the network.
They lock in two things that previously cost the workflow its 15-minute
job budget:
  1. candidate URLs are probed in parallel, not one after another;
  2. the whole MOI phase is capped by MOI_PHASE_BUDGET.
"""

import time
import unittest

import utils
from utils import find_moi_paper_universal, get_mdn_backup_papers


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


if __name__ == "__main__":
    unittest.main()
