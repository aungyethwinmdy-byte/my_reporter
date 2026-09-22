"""Tests for fetch_independent_news — the independent-media ingestion pipeline.

This module had no functional test coverage at all. The bugs it hid were the
kind that stay invisible because the pipeline still "succeeds":

  * RFA Burmese was configured with t.me URLs that resolve to a user *contact*
    page, not a channel preview. It returned HTTP 200 with zero message blocks,
    so fetch_from_telegram returned [] and the run looked healthy while RFA
    contributed nothing — despite being listed in /sources as monitored.
  * save_articles_to_supabase reported the *candidate* count when nothing was
    inserted, because it treated an empty response.data (the normal "all
    duplicates" case) as a failure to read.

No network, no Supabase, no credentials.
"""

import hashlib
import unittest
from datetime import datetime
from email.utils import parsedate_to_datetime
from unittest.mock import MagicMock, patch

import fetch_independent_news as fin


def _resp(text="", status=200, content=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.content = content if content is not None else text.encode("utf-8")
    return r


# --- fixtures -------------------------------------------------------------

CONTACT_PAGE = """
<!doctype html><html><head>
<title>Telegram: Contact @RFA_Burmese</title></head>
<body><div class="tgme_page">Download If you have Telegram, you can contact
@RFA_Burmese right away. Send Message</div></body></html>
"""

CHANNEL_PAGE = """
<!doctype html><html><head><title>The Irrawaddy - Burmese – Telegram</title></head>
<body>
  <div class="tgme_widget_message">
    <div class="tgme_widget_message_text">
      ရခိုင်နှင့် ကချင်သာမက ချင်းတောင်ပိုင်းကိုပါ စစ်ဆင်ရန် စစ်တပ် ပြင်ဆင်
      <a href="https://burma.irrawaddy.com/article/2026/09/20/999111.html">အပြည့်အစုံ</a>
    </div>
    <a class="tgme_widget_message_date" href="https://t.me/theirrawaddy/67200">
      <time datetime="2026-09-20T08:30:00+00:00">08:30</time>
    </a>
  </div>
  <div class="tgme_widget_message">
    <div class="tgme_widget_message_text">
      ကုလသမဂ္ဂရုံးချုပ်ရှေ့ သံအမတ်ကြီး ထောက်ခံပွဲ ပြုလုပ်
    </div>
    <a class="tgme_widget_message_date" href="https://t.me/theirrawaddy/67216">
      <time datetime="2026-09-20T09:15:00+00:00">09:15</time>
    </a>
  </div>
</body></html>
"""

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>BBC Burmese</title>
<item>
  <title>စက်တင်ဘာ ၂၀ ရက် သတင်းအနှစ်ချုပ်</title>
  <link>https://www.bbc.com/burmese/articles/c64gv8jp5nl0o</link>
  <description>&lt;p&gt;သတင်းအကျဉ်းချုပ်&lt;/p&gt;</description>
  <pubDate>Sat, 20 Sep 2026 04:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


class CleanHtmlTextTests(unittest.TestCase):
    def test_strips_tags_and_collapses_whitespace(self):
        out = fin.clean_html_text("<p>hello</p>\n\n  <b>world</b>")
        self.assertEqual(out, "hello world")

    def test_removes_script_and_style_content(self):
        out = fin.clean_html_text(
            "<div>keep<script>var x=1;</script><style>.a{}</style></div>"
        )
        self.assertEqual(out, "keep")

    def test_empty_input(self):
        self.assertEqual(fin.clean_html_text(""), "")
        self.assertEqual(fin.clean_html_text(None), "")


class CalculateHashTests(unittest.TestCase):
    def test_matches_sha256_of_utf8(self):
        text = "မြန်မာစာ"
        self.assertEqual(fin.calculate_hash(text), hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_is_deterministic(self):
        self.assertEqual(fin.calculate_hash("abc"), fin.calculate_hash("abc"))


class ParsePublishedDateTests(unittest.TestCase):
    def test_parses_rfc2822(self):
        self.assertEqual(
            fin.parse_published_date({"published": "Sat, 20 Sep 2026 04:00:00 GMT"}),
            "2026-09-20",
        )

    def test_late_utc_posts_roll_over_to_the_next_myanmar_day(self):
        """Myanmar is UTC+6:30, so 20:00 GMT is already tomorrow locally.

        Storing the raw UTC date put every post in the 17:30-23:59 UTC window on
        the wrong day — about a quarter of the clock — which silently excluded
        those articles from a today-scoped /compare or daily briefing.
        """
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 20:00:00 GMT"}),
            "2026-09-22",
        )
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 23:59:00 GMT"}),
            "2026-09-22",
        )

    def test_the_myanmar_day_boundary_is_17_30_utc(self):
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 17:29:00 GMT"}),
            "2026-09-21",
        )
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 17:31:00 GMT"}),
            "2026-09-22",
        )

    def test_matches_the_telegram_paths_conversion(self):
        """Both ingestion paths must agree on which day an article belongs to."""
        raw = "Mon, 21 Sep 2026 20:00:00 GMT"
        expected = (
            parsedate_to_datetime(raw).astimezone(fin.MYANMAR_TZ).strftime("%Y-%m-%d")
        )
        self.assertEqual(fin.parse_published_date({"published": raw}), expected)

    def test_non_gmt_offsets_are_converted_not_assumed(self):
        # 20:00 in a UTC-4 feed is 00:00 UTC the next day, then 06:30 MMT.
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 20:00:00 -0400"}),
            "2026-09-22",
        )

    def test_a_feed_without_a_zone_is_treated_as_utc(self):
        self.assertEqual(
            fin.parse_published_date({"published": "Mon, 21 Sep 2026 20:00:00"}),
            "2026-09-22",
        )

    def test_falls_back_to_today_on_garbage(self):
        today = datetime.now(fin.MYANMAR_TZ).strftime("%Y-%m-%d")
        self.assertEqual(fin.parse_published_date({"published": "not a date"}), today)

    def test_falls_back_when_missing(self):
        today = datetime.now(fin.MYANMAR_TZ).strftime("%Y-%m-%d")
        self.assertEqual(fin.parse_published_date({}), today)


class TelegramParsingTests(unittest.TestCase):
    def test_parses_channel_page_and_prefers_the_article_link(self):
        with patch.object(fin.requests, "get", return_value=_resp(CHANNEL_PAGE)):
            arts = fin.fetch_from_telegram("The Irrawaddy", ["https://t.me/s/theirrawaddy"])
        self.assertEqual(len(arts), 2)
        # First message carries an outbound article link -> that is the url.
        self.assertEqual(
            arts[0]["url"], "https://burma.irrawaddy.com/article/2026/09/20/999111.html"
        )
        self.assertIn("ရခိုင်", arts[0]["headline"])
        # Second message has no article link -> t.me fallback.
        self.assertEqual(arts[1]["url"], "https://t.me/theirrawaddy/67216")
        self.assertTrue(arts[0]["content_hash"])

    def test_contact_page_yields_nothing(self):
        """The RFA bug: a user account looks like a successful empty fetch."""
        with patch.object(fin.requests, "get", return_value=_resp(CONTACT_PAGE)):
            arts = fin.fetch_from_telegram("RFA Burmese", ["https://t.me/s/RFA_Burmese"])
        self.assertEqual(arts, [])

    def test_contact_page_is_logged_as_an_error(self):
        with patch.object(fin.requests, "get", return_value=_resp(CONTACT_PAGE)), patch.object(
            fin.logger, "error"
        ) as err:
            fin.fetch_from_telegram("RFA Burmese", ["https://t.me/s/RFA_Burmese"])
        self.assertTrue(err.called, "a contact page must be reported, not silently swallowed")
        self.assertIn("contact", str(err.call_args[0][0]).lower())

    def test_falls_through_to_the_second_mirror_url(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if "first" in url:
                return _resp("", status=404)
            return _resp(CHANNEL_PAGE)

        with patch.object(fin.requests, "get", side_effect=fake_get):
            arts = fin.fetch_from_telegram("The Irrawaddy", ["https://x/first", "https://x/second"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(arts), 2)

    def test_unreachable_mirrors_return_empty(self):
        with patch.object(fin.requests, "get", side_effect=RuntimeError("boom")):
            self.assertEqual(fin.fetch_from_telegram("X", ["https://x/1", "https://x/2"]), [])

    def test_published_date_uses_myanmar_timezone(self):
        """08:30 UTC is 15:00 in Yangon — same calendar day, but verify parsing."""
        with patch.object(fin.requests, "get", return_value=_resp(CHANNEL_PAGE)):
            arts = fin.fetch_from_telegram("The Irrawaddy", ["https://t.me/s/theirrawaddy"])
        self.assertEqual(arts[0]["published_date"], "2026-09-20")

    def test_short_headlines_are_skipped(self):
        page = """
        <html><body><div class="tgme_widget_message">
          <div class="tgme_widget_message_text">ab</div>
        </div></body></html>
        """
        with patch.object(fin.requests, "get", return_value=_resp(page)):
            self.assertEqual(fin.fetch_from_telegram("X", ["https://t.me/s/x"]), [])


class RssParsingTests(unittest.TestCase):
    def test_parses_feed_and_hashes_headline_plus_body(self):
        with patch.object(fin.requests, "get", return_value=_resp(RSS_XML)), patch.object(
            fin, "extract_full_article_body", return_value=""
        ):
            arts = fin.fetch_from_rss("BBC Burmese", fin.RSS_FEEDS["BBC Burmese"])
        self.assertEqual(len(arts), 1)
        self.assertEqual(arts[0]["published_date"], "2026-09-20")
        self.assertIn("bbc.com", arts[0]["url"])
        self.assertTrue(arts[0]["content_hash"])
        # Falls back to the summary when the full body is too short.
        self.assertIn("သတင်းအကျဉ်းချုပ်", arts[0]["body_text"])

    def test_non_200_returns_empty(self):
        with patch.object(fin.requests, "get", return_value=_resp("", status=503)):
            self.assertEqual(fin.fetch_from_rss("BBC Burmese", "https://x/feed"), [])


class SaveArticlesTests(unittest.TestCase):
    def test_empty_input_short_circuits(self):
        self.assertEqual(fin.save_articles_to_supabase([]), 0)

    def test_returns_inserted_count_not_candidate_count(self):
        """All-duplicates is a normal run, not a failure to read.

        The old code did `len(response.data) if response.data else len(articles)`,
        so an empty response (every candidate already present) was reported as
        "N articles processed".
        """
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
        with patch.object(fin, "get_supabase_client", return_value=client):
            result = fin.save_articles_to_supabase(
                [{"url": "https://x/1"}, {"url": "https://x/2"}, {"url": "https://x/3"}]
            )
        self.assertEqual(result, 0, "nothing was inserted, so nothing should be reported")

    def test_returns_real_insert_count(self):
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.return_value = MagicMock(
            data=[{"id": 1}, {"id": 2}]
        )
        with patch.object(fin, "get_supabase_client", return_value=client):
            result = fin.save_articles_to_supabase([{"url": "https://x/1"}, {"url": "https://x/2"}])
        self.assertEqual(result, 2)

    def test_uses_url_as_the_conflict_target(self):
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[])
        with patch.object(fin, "get_supabase_client", return_value=client):
            fin.save_articles_to_supabase([{"url": "https://x/1"}])
        kwargs = client.table.return_value.upsert.call_args[1]
        self.assertEqual(kwargs["on_conflict"], "url")

    def test_missing_client_returns_zero(self):
        with patch.object(fin, "get_supabase_client", return_value=None):
            self.assertEqual(fin.save_articles_to_supabase([{"url": "https://x/1"}]), 0)

    def test_supabase_error_is_swallowed_and_reported_as_zero(self):
        client = MagicMock()
        client.table.return_value.upsert.return_value.execute.side_effect = RuntimeError("boom")
        with patch.object(fin, "get_supabase_client", return_value=client):
            self.assertEqual(fin.save_articles_to_supabase([{"url": "https://x/1"}]), 0)


class FetchAllTests(unittest.TestCase):
    def test_warns_loudly_when_a_source_yields_nothing(self):
        """A dead source must not be indistinguishable from a quiet news day."""
        with patch.object(fin, "fetch_from_rss", return_value=[]), patch.object(
            fin, "fetch_from_telegram", return_value=[]
        ), patch.object(fin, "save_articles_to_supabase", return_value=0), patch.object(
            fin.logger, "warning"
        ) as warn:
            fin.fetch_all()
        messages = " ".join(str(c[0][0]) for c in warn.call_args_list)
        self.assertIn("ZERO", messages)

    def test_no_warning_when_every_source_yields_something(self):
        with patch.object(fin, "fetch_from_rss", return_value=[{"url": "u"}]), patch.object(
            fin, "fetch_from_telegram", return_value=[{"url": "u2"}]
        ), patch.object(fin, "save_articles_to_supabase", return_value=2), patch.object(
            fin.logger, "warning"
        ) as warn:
            fin.fetch_all()
        self.assertFalse(warn.called)


if __name__ == "__main__":
    unittest.main()
