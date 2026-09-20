"""Tests for numeric_intelligence_engine.

IMPORTANT CONTEXT — this module is currently DEAD CODE.
Nothing imports it: the only references anywhere are the module-name lists in
tests/test_main.py and tests/test_wiring.py, which just assert it imports
cleanly. The bot's live numeric route uses telegram_bot.query_newspaper_numbers
and report_formatter.format_numeric_dashboard instead.

That makes the bugs below latent rather than active, but it also means the
module has silently drifted away from the code that replaced it — including a
second, DIVERGENT copy of is_numeric_or_price_query. These tests pin the
behaviour so the module is safe if it is ever revived, and so the divergence is
visible. See the note at the bottom of this file.

No network, no Supabase, no credentials.
"""

import unittest
from unittest.mock import MagicMock

from numeric_intelligence_engine import (
    _format_diff,
    _sanitize_term,
    build_precision_comparison_table,
    is_numeric_or_price_query,
    query_exact_numbers,
)


def row(context, date, value, unit="ကျပ်", original_value=None, **extra):
    r = {"context": context, "publication_date": date, "value": value, "unit": unit}
    if original_value is not None:
        r["original_value"] = original_value
    r.update(extra)
    return r


class SanitizeTermTests(unittest.TestCase):
    def test_strips_postgrest_metacharacters(self):
        self.assertEqual(_sanitize_term("ရွှေ,ဒေါ်လာ"), "ရွှေဒေါ်လာ")
        self.assertEqual(_sanitize_term("a%"), "a")
        self.assertEqual(_sanitize_term("x(y)z"), "xyz")
        self.assertEqual(_sanitize_term("a;b'c\"d"), "abcd")

    def test_keeps_ordinary_text(self):
        self.assertEqual(_sanitize_term("Octane 92"), "Octane 92")

    def test_handles_none_and_whitespace(self):
        self.assertEqual(_sanitize_term(None), "")
        self.assertEqual(_sanitize_term("   "), "")


class FormatDiffTests(unittest.TestCase):
    def test_large_integers_are_not_scientific(self):
        """:g switched to 1e+06 at a million and capped at 6 significant digits.

        Gold moves in these papers routinely reach seven digits, so the old
        formatting printed "+1.5e+06" and "+1.23457e+06" to real readers.
        """
        self.assertEqual(_format_diff(1_500_000), "1,500,000")
        self.assertEqual(_format_diff(1_234_567), "1,234,567")
        self.assertEqual(_format_diff(3_200_000), "3,200,000")

    def test_small_values_are_plain(self):
        self.assertEqual(_format_diff(100), "100")
        self.assertEqual(_format_diff(99_999), "99,999")

    def test_negatives_keep_their_sign(self):
        self.assertEqual(_format_diff(-2_500_000), "-2,500,000")
        self.assertEqual(_format_diff(-100), "-100")

    def test_fractional_values_show_at_most_two_places(self):
        """Trailing zeros are trimmed, because a float carries no scale.

        1500.5 cannot know it "meant" 1500.50, and a whole-number float such as
        1500.0 never reaches this branch — it is rendered by the integer path.
        So the contract is "at most two decimals", not "exactly two".
        """
        self.assertEqual(_format_diff(1500.5), "1,500.5")
        self.assertEqual(_format_diff(0.25), "0.25")
        self.assertEqual(_format_diff(1500.05), "1,500.05")
        self.assertEqual(_format_diff(-0.5), "-0.5")


class QueryExactNumbersTests(unittest.TestCase):
    def _client(self):
        captured = []

        class FakeQuery:
            def select(self, *a, **k):
                return self

            def in_(self, *a, **k):
                return self

            def or_(self, clause):
                captured.append(clause)
                return self

            def order(self, *a, **k):
                return self

            def execute(self):
                return MagicMock(data=[{"ok": True}])

        client = MagicMock()
        client.from_.return_value = FakeQuery()
        return client, captured

    def test_comma_in_term_does_not_split_the_or_clause(self):
        client, captured = self._client()
        query_exact_numbers(client, "ရွှေ,ဒေါ်လာ", ["2026-09-20"])
        clause = captured[0]
        # Exactly the three intended conditions, not five.
        self.assertEqual(clause.count("ilike"), 3)
        self.assertNotIn(",ဒေါ်လာ", clause)

    def test_percent_is_not_a_wildcard_injection(self):
        client, captured = self._client()
        query_exact_numbers(client, "a%", ["2026-09-20"])
        self.assertNotIn("%a%%", captured[0])

    def test_blank_term_skips_the_query_entirely(self):
        client, captured = self._client()
        self.assertEqual(query_exact_numbers(client, "   ", ["2026-09-20"]), [])
        self.assertEqual(captured, [], "a blank term must not reach Supabase")

    def test_returns_rows_from_the_response(self):
        client, _ = self._client()
        self.assertEqual(query_exact_numbers(client, "ရွှေ", ["2026-09-20"]), [{"ok": True}])


class ComparisonTableTests(unittest.TestCase):
    def test_gold_difference_is_readable(self):
        rows = [
            row("အကြွေစေ့ရွှေ", "2026-09-19", "3200000", original_value="၃,၂၀၀,၀၀၀"),
            row("အကြွေစေ့ရွှေ", "2026-09-20", "4700000", original_value="၄,၇၀၀,၀၀၀"),
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "ရွှေဈေး")
        self.assertIn("+1,500,000", out)
        self.assertNotIn("e+06", out)

    def test_fall_shows_a_negative_difference(self):
        rows = [
            row("Octane 92", "2026-09-19", "2600"),
            row("Octane 92", "2026-09-20", "2500"),
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "ဆီဈေး")
        self.assertIn("-100", out)

    def test_unchanged_is_labelled(self):
        rows = [
            row("Octane 92", "2026-09-19", "2500"),
            row("Octane 92", "2026-09-20", "2500"),
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "ဆီဈေး")
        self.assertIn("မပြောင်းလဲ", out)

    def test_missing_day_is_labelled_not_crashed(self):
        rows = [row("Octane 92", "2026-09-20", "2500")]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "ဆီဈေး")
        self.assertIn("မပါရှိပါ", out)

    def test_row_without_context_does_not_crash(self):
        """A missing "context" raised KeyError and killed the whole report."""
        rows = [
            {"publication_date": "2026-09-19", "value": "100", "unit": "ကျပ်"},
            {"publication_date": "2026-09-20", "value": "200", "unit": "ကျပ်"},
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "T")
        self.assertIn("တိကျသော", out)

    def test_row_without_value_degrades_gracefully(self):
        """KeyError was not in the except tuple, so it escaped."""
        rows = [
            {"context": "X", "publication_date": "2026-09-19", "unit": "ကျပ်"},
            {"context": "X", "publication_date": "2026-09-20", "unit": "ကျပ်"},
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "T")
        self.assertIn("မတွက်ချက်နိုင်ပါ", out)

    def test_non_numeric_value_degrades_gracefully(self):
        rows = [
            row("X", "2026-09-19", "မပါရှိပါ"),
            row("X", "2026-09-20", "2500"),
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "T")
        self.assertIn("မတွက်ချက်နိုင်ပါ", out)

    def test_empty_rows_returns_empty_string(self):
        self.assertEqual(
            build_precision_comparison_table([], "2026-09-19", "2026-09-20", "T"), ""
        )

    def test_source_evidence_is_appended(self):
        rows = [
            row("X", "2026-09-20", "2500", source_text="Octane 92 တစ်လီတာ ၂,၅၀၀ ကျပ်",
                headline="ဆီဈေး"),
        ]
        out = build_precision_comparison_table(rows, "2026-09-19", "2026-09-20", "T")
        self.assertIn("မူရင်း သတင်းစာအထောက်အထား", out)
        self.assertIn("Octane 92", out)


class NumericQueryDetectionTests(unittest.TestCase):
    def test_detects_price_questions(self):
        self.assertTrue(is_numeric_or_price_query("ဒီနေ့ ရွှေဈေး ဘယ်လောက်လဲ"))
        self.assertTrue(is_numeric_or_price_query("octane 92 price"))

    def test_ignores_ordinary_news_questions(self):
        self.assertFalse(is_numeric_or_price_query("တောင်ငူ ရေကြီးမှု အခြေအနေ"))

    def test_diverges_from_the_telegram_bot_copy(self):
        """Documented divergence between two same-named functions.

        telegram_bot.is_numeric_or_price_query (the live one) requires a
        commodity-ish keyword; this copy also matches bare "ဘယ်လောက်", "တက်",
        "ကျ", "ဈေး". A query like "ရေကြီးမှု ဘယ်လောက်ကြာမလဲ" is therefore
        numeric here but not in the bot. Kept as a test so the split is not
        rediscovered the hard way if this module is ever wired in.
        """
        from telegram_bot import is_numeric_or_price_query as live

        query = "ရေကြီးမှု ဘယ်လောက်ကြာမလဲ"
        self.assertTrue(is_numeric_or_price_query(query))
        self.assertFalse(live(query), "the live bot copy is narrower by design")


if __name__ == "__main__":
    unittest.main()
