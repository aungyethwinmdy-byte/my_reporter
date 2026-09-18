"""Tests for report_formatter — the numeric price/commodity dashboard.

This module had zero test coverage before these tests were added, and it is the
component that renders the actual kyat figures a reader sees. Everything here
targets either a formatting correctness question (does the number the user
reads match the number the newspaper printed?) or an editorial question (does
the summary sentence describe what the table shows?).

No network, no API keys.
"""

import random
import unittest

from report_formatter import (
    clean_number_value,
    format_numeric_dashboard,
    get_category_icon,
    normalize_digits,
    simplify_item_name,
    to_burmese_digits,
)


def row(context, date, value, unit="ကျပ်", headline=None, original_value=None):
    """Build a newspaper_numbers-shaped row."""
    r = {"context": context, "publication_date": date, "value": value, "unit": unit}
    if headline is not None:
        r["headline"] = headline
    if original_value is not None:
        r["original_value"] = original_value
    return r


class DigitNormalizationTests(unittest.TestCase):
    def test_english_to_burmese_integers(self):
        self.assertEqual(to_burmese_digits(2500), "၂,၅၀၀")
        self.assertEqual(to_burmese_digits("2500"), "၂,၅၀၀")

    def test_whole_float_drops_the_decimal_point(self):
        self.assertEqual(to_burmese_digits(2500.0), "၂,၅၀၀")

    def test_burmese_input_round_trips(self):
        self.assertEqual(to_burmese_digits("၃,၀၅၀"), "၃,၀၅၀")

    def test_trailing_zero_in_a_string_is_significant(self):
        """1500.50 is fifteen hundred kyat and fifty pyas, not "...point five"."""
        self.assertEqual(to_burmese_digits("1500.50"), "၁,၅၀၀.၅၀")
        self.assertEqual(to_burmese_digits("10.10"), "၁၀.၁၀")

    def test_non_integral_float_trims_to_two_places(self):
        self.assertEqual(to_burmese_digits(1500.55), "၁,၅၀၀.၅၅")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(to_burmese_digits(None), "")
        self.assertEqual(to_burmese_digits(""), "")

    def test_bool_is_not_rendered_as_a_number(self):
        """bool subclasses int; "၁" for True would be nonsense."""
        self.assertEqual(to_burmese_digits(True), "True")

    def test_non_numeric_text_passes_through(self):
        self.assertEqual(to_burmese_digits("abc"), "abc")
        # A range is not a single number, so digits are converted in place
        # rather than grouped and rounded.
        self.assertEqual(to_burmese_digits("2500-2600"), "၂၅၀၀-၂၆၀၀")

    def test_normalize_digits_handles_none(self):
        self.assertEqual(normalize_digits(None), "")
        self.assertEqual(normalize_digits("၂၅၀၀"), "2500")


class CleanNumberValueTests(unittest.TestCase):
    def test_extracts_plain_and_burmese_values(self):
        self.assertEqual(clean_number_value("2500"), 2500.0)
        self.assertEqual(clean_number_value("၂၅၀၀"), 2500.0)

    def test_strips_commas_and_surrounding_text(self):
        self.assertEqual(clean_number_value("2,500"), 2500.0)
        self.assertEqual(clean_number_value("2500 ကျပ်"), 2500.0)

    def test_negative_and_absent_values(self):
        self.assertEqual(clean_number_value("-2500"), -2500.0)
        self.assertIsNone(clean_number_value(None))
        self.assertIsNone(clean_number_value("မပါရှိပါ"))


class SimplifyItemNameTests(unittest.TestCase):
    def test_strips_the_reference_price_suffix(self):
        self.assertEqual(simplify_item_name("ရွှေရည်ညွှန်းဈေး"), "ရွှေ")
        self.assertEqual(
            simplify_item_name("စက်သုံးဆီရည်ညွှန်းလက်ကားဈေးနှုန်းများ"), "စက်သုံးဆီ"
        )

    def test_short_suffix_still_strips_after_long_one_is_tried(self):
        """The old chained .replace() was order-sensitive.

        ``ရည်ညွှန်းဈေး`` is a suffix of ``ရည်ညွှန်းဈေးနှုန်း``; once the longer
        pattern consumed the text, the shorter replacement had nothing left to
        match and the label survived in the group key.
        """
        self.assertEqual(simplify_item_name("ရွှေရည်ညွှန်းဈေးနှုန်း"), "ရွှေ")
        self.assertEqual(simplify_item_name("ရွှေရည်ညွှန်းလက်ကားဈေး"), "ရွှေ")

    def test_distinct_bare_labels_do_not_collapse_together(self):
        """A name that is only a label must not become an empty group key.

        Returning "" here would merge every label-only row into one group and
        silently combine unrelated commodities.
        """
        a = simplify_item_name("ရည်ညွှန်းဈေး")
        b = simplify_item_name("ရည်ညွှန်းလက်ကားဈေး")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, "")

    def test_special_names_are_beautified(self):
        self.assertEqual(simplify_item_name("Diesel"), "Diesel (ရိုးရိုးဒီဇယ်)")
        self.assertEqual(simplify_item_name("Premium Diesel"), "Premium Diesel (ပရီမီယမ်)")
        self.assertEqual(simplify_item_name("စံချိန်မီရွှေ"), "စံချိန်မီရွှေ (၁၆ ပဲရည်)")

    def test_empty_and_none(self):
        self.assertEqual(simplify_item_name(""), "")
        self.assertEqual(simplify_item_name(None), "")


class CategoryIconTests(unittest.TestCase):
    def test_fuel_gold_currency_and_agriculture(self):
        self.assertEqual(get_category_icon("", ["Octane 92"]), "⛽")
        self.assertEqual(get_category_icon("", ["ရွှေ"]), "🪙")
        self.assertEqual(get_category_icon("", ["ဒေါ်လာ"]), "💵")
        self.assertEqual(get_category_icon("", ["ဆန်"]), "🌾")

    def test_unknown_falls_back_to_generic_chart(self):
        self.assertEqual(get_category_icon("", ["something"]), "📊")


class SingleDayDashboardTests(unittest.TestCase):
    def test_empty_rows_returns_empty_string(self):
        self.assertEqual(format_numeric_dashboard([], ["2026-09-18"]), "")

    def test_no_dates_returns_empty_string(self):
        """Must not raise IndexError on a malformed call."""
        self.assertEqual(format_numeric_dashboard([row("Octane 92", "2026-09-18", "2500")], []), "")

    def test_renders_value_with_unit(self):
        out = format_numeric_dashboard(
            [row("Octane 92", "2026-09-18", "2500")], ["2026-09-18"]
        )
        self.assertIn("၂,၅၀၀", out)
        self.assertIn("Octane 92", out)

    def test_prefers_original_value_for_display(self):
        """original_value keeps the newspaper's own rendering."""
        out = format_numeric_dashboard(
            [row("Octane 92", "2026-09-18", "3050", original_value="၃,၀၅၀")],
            ["2026-09-18"],
        )
        self.assertIn("၃,၀၅၀", out)

    def test_missing_value_is_labelled_not_blank(self):
        out = format_numeric_dashboard(
            [row("Octane 92", "2026-09-18", None)], ["2026-09-18"]
        )
        self.assertIn("မပါရှိပါ", out)


class ComparisonDashboardTests(unittest.TestCase):
    def _pair(self, y, t, context="Octane 92", unit="ကျပ်"):
        return [
            row(context, "2026-09-17", y, unit),
            row(context, "2026-09-18", t, unit),
        ]

    def test_rise_is_shown_as_up(self):
        out = format_numeric_dashboard(self._pair("2500", "2600"), ["2026-09-17", "2026-09-18"])
        self.assertIn("+၁၀၀", out)
        self.assertIn("(တက်)", out)

    def test_fall_is_shown_as_down(self):
        out = format_numeric_dashboard(self._pair("2600", "2500"), ["2026-09-17", "2026-09-18"])
        self.assertIn("-၁၀၀", out)
        self.assertIn("(ကျ)", out)

    def test_flat_is_labelled_unchanged(self):
        out = format_numeric_dashboard(self._pair("2500", "2500"), ["2026-09-17", "2026-09-18"])
        self.assertIn("မပြောင်းလဲ", out)

    def test_three_dates_does_not_silently_drop_the_extra_day(self):
        """len(dates) > 2 used to fall into the single-day branch.

        The dashboard then reported only the first date and discarded the rest
        of the series, which is worse than an error because it looks plausible.
        """
        rows = [
            row("Octane 92", "2026-09-16", "2400"),
            row("Octane 92", "2026-09-17", "2500"),
            row("Octane 92", "2026-09-18", "2600"),
        ]
        out = format_numeric_dashboard(rows, ["2026-09-16", "2026-09-17", "2026-09-18"])
        # The two most recent days are compared, not the first day shown alone.
        self.assertIn("2026-09-17", out)
        self.assertIn("2026-09-18", out)
        self.assertIn("+၁၀၀", out)

    def test_direction_is_not_dominated_by_a_large_denomination(self):
        """Fuel fell while gold rose; the summary must not claim fuel rose.

        The old code summed raw deltas across commodities. Gold's +200 on a
        3,000,000 base swamped fuel's -100 on a 2,500 base, so the footer said
        prices climbed even though the fuel price had dropped.
        """
        rows = [
            row("Octane 92", "2026-09-17", "2500"),
            row("Octane 92", "2026-09-18", "2400"),
            row("Gold", "2026-09-17", "3000000"),
            row("Gold", "2026-09-18", "3000200"),
        ]
        out = format_numeric_dashboard(rows, ["2026-09-17", "2026-09-18"])
        status = [ln for ln in out.split("\n") if "အခြေအနေ" in ln][0]
        # Mixed movement is reported as mixed, not as a blanket rise.
        self.assertIn("ဈေးတက်", status)
        self.assertIn("ဈေးကျ", status)

    def test_all_items_falling_reports_a_fall(self):
        rows = [
            row("Octane 92", "2026-09-17", "2500"),
            row("Octane 92", "2026-09-18", "2400"),
            row("Diesel", "2026-09-17", "2200"),
            row("Diesel", "2026-09-18", "2100"),
        ]
        out = format_numeric_dashboard(rows, ["2026-09-17", "2026-09-18"])
        status = [ln for ln in out.split("\n") if "အခြေအနေ" in ln][0]
        self.assertIn("ကျဆင်း", status)
        self.assertNotIn("မြင့်တက်", status)

    def test_source_citation_is_stable_under_row_reordering(self):
        """Supabase row order is not stable; the footer must not flip papers.

        source_headlines was a set and the code took list(...)[0], so identical
        data could cite မြန်မာ့အလင်း on one run and ကြေးမုံ on the next.
        """
        base = [
            row("Octane 92", "2026-09-17", "2500", headline="MAL-P2"),
            row("Octane 92", "2026-09-18", "2600", headline="KM-P2"),
            row("Diesel", "2026-09-17", "2200", headline="MAL-P2"),
            row("Diesel", "2026-09-18", "2250", headline="KM-P2"),
        ]
        seen = set()
        for seed in range(1, 9):
            shuffled = list(base)
            random.Random(seed).shuffle(shuffled)
            out = format_numeric_dashboard(shuffled, ["2026-09-17", "2026-09-18"])
            seen.add([ln for ln in out.split("\n") if "အရင်းအမြစ်" in ln][0])
        self.assertEqual(len(seen), 1, f"citation varied across row orders: {seen}")

    def test_missing_day_is_reported_per_item(self):
        """If only one of the two days has a record, say so for that item."""
        out = format_numeric_dashboard(
            [row("Octane 92", "2026-09-18", "2600")], ["2026-09-17", "2026-09-18"]
        )
        self.assertIn("မပါရှိပါ", out)

    def test_dashes_in_input_do_not_crash(self):
        out = format_numeric_dashboard(
            [row("Octane 92", "2026-09-18", "မပါရှိပါ")], ["2026-09-18"]
        )
        self.assertIn("Octane 92", out)


if __name__ == "__main__":
    unittest.main()
