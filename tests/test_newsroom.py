"""Unit tests for telegram_bot's deterministic (non-AI) logic.

Only pure helpers are covered — no network, no API keys, no Supabase.
If you rename/move any of these functions, update this file too.
"""

import unittest

from telegram_bot import (
    build_precision_numeric_report,
    clean_number_value,
    detect_commodity_context,
    get_myanmar_dates,
    is_numeric_or_price_query,
    normalize_digits,
    split_message_text,
)


class DateHelperTests(unittest.TestCase):
    def test_today_and_yesterday_are_iso_and_ordered(self):
        today, yesterday = get_myanmar_dates()
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(yesterday, r"^\d{4}-\d{2}-\d{2}$")
        self.assertLess(yesterday, today)


class NumericHelperTests(unittest.TestCase):
    def test_normalize_digits_converts_burmese_numerals(self):
        self.assertEqual(normalize_digits("၂၀၂၆ ခုနှစ်"), "2026 ခုနှစ်")
        self.assertEqual(normalize_digits(""), "")
        self.assertEqual(normalize_digits(None), "")

    def test_clean_number_value_parses_formatted_prices(self):
        self.assertEqual(clean_number_value("2,800"), 2800.0)
        self.assertEqual(clean_number_value("၂,၈၀၀ ကျပ်"), 2800.0)
        self.assertEqual(clean_number_value("12.5"), 12.5)
        self.assertEqual(clean_number_value("2.800"), 2.8)

    def test_clean_number_value_returns_none_for_no_digits(self):
        self.assertIsNone(clean_number_value(None))
        self.assertIsNone(clean_number_value(""))
        self.assertIsNone(clean_number_value("ဈေးနှုန်း မပါရှိပါ"))


class QueryClassificationTests(unittest.TestCase):
    def test_price_queries_are_detected(self):
        self.assertTrue(is_numeric_or_price_query("စက်သုံးဆီ ဈေးနှုန်း ဘယ်လောက်လဲ"))
        self.assertTrue(is_numeric_or_price_query("ဒီဇယ် တစ်လီတာ ဈေး"))
        self.assertTrue(is_numeric_or_price_query("မနေ့က နဲ့ ဒီနေ့ နှိုင်းယှဉ်ပြပါ"))
        self.assertTrue(is_numeric_or_price_query("ရွှေဈေး တက်လား"))

    def test_plain_news_queries_are_not_numeric(self):
        self.assertFalse(is_numeric_or_price_query("လွှတ်တော် အစည်းအဝေး သတင်း"))
        self.assertFalse(is_numeric_or_price_query(""))

    def test_commodity_detection(self):
        self.assertEqual(detect_commodity_context("စက်သုံးဆီ ဈေး"), "စက်သုံးဆီ")
        self.assertEqual(detect_commodity_context("Octane 92 ဈေးနှုန်း"), "စက်သုံးဆီ")
        self.assertEqual(detect_commodity_context("ဒီဇယ် တစ်လီတာ"), "စက်သုံးဆီ")
        self.assertEqual(detect_commodity_context("ရွှေဈေး ဘယ်လောက်လဲ"), "ရွှေ")
        self.assertEqual(detect_commodity_context("ဒေါ်လာ ငွေလဲနှုန်း"), "ဒေါ်လာ")
        self.assertEqual(detect_commodity_context("စပါး အထွက်နှုန်း"), "စပါး")

    def test_commodity_detection_falls_back_to_empty(self):
        self.assertEqual(detect_commodity_context("လွှတ်တော် အစည်းအဝေး"), "")


class MessageSplitTests(unittest.TestCase):
    def test_short_message_is_not_split(self):
        self.assertEqual(split_message_text("hello"), ["hello"])

    def test_long_message_is_split_under_limit(self):
        text = "\n\n".join(["ပိုဒ် " * 400 for _ in range(20)])
        chunks = split_message_text(text, max_length=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1000)

    def test_single_very_long_paragraph_is_split(self):
        chunks = split_message_text("က" * 9000, max_length=3500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3500)


class PrecisionReportTests(unittest.TestCase):
    def test_empty_rows_produce_empty_report(self):
        self.assertEqual(
            build_precision_numeric_report([], ["2026-09-14"], "ဈေး"),
            "",
        )

    def test_comparison_report_contains_both_dates_and_change(self):
        rows = [
            {
                "publication_date": "2026-09-13",
                "context": "Octane 92",
                "value": 2800.0,
                "original_value": "2,800",
                "unit": "ကျပ်",
                "headline": "စက်သုံးဆီ ရည်ညွှန်းဈေး",
            },
            {
                "publication_date": "2026-09-14",
                "context": "Octane 92",
                "value": 3000.0,
                "original_value": "3,000",
                "unit": "ကျပ်",
                "headline": "စက်သုံးဆီ ရည်ညွှန်းဈေး",
            },
        ]
        report = build_precision_numeric_report(
            data_rows=rows,
            dates=["2026-09-13", "2026-09-14"],
            user_query="မနေ့က နဲ့ ဒီနေ့ စက်သုံးဆီဈေး နှိုင်းယှဉ်ပြပါ",
            title_override="စက်သုံးဆီ ဈေးနှုန်းများ",
        )
        self.assertIsInstance(report, str)
        self.assertNotEqual(report, "")
        self.assertIn("2026-09-13", report)
        self.assertIn("2026-09-14", report)
        self.assertIn("Octane 92", report)
        # Difference is 200 and must be computed, not hallucinated.
        self.assertIn("200", report)


if __name__ == "__main__":
    unittest.main()
