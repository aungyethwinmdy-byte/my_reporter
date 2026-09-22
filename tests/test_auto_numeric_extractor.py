"""Tests for auto_numeric_extractor — the module that fills `newspaper_numbers`.

This module supplies every figure the price dashboard later renders, yet it had
no dedicated test file: the only coverage was one model-name assertion inside
test_gemini_config.py. That is the same blind spot report_formatter.py had while
it was rendering the kyat figures readers actually read.

No network, no Supabase, no credentials — Gemini is replaced by a tiny fake.
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gemini_config
from auto_numeric_extractor import (
    _parse_extracted_json,
    clean_number,
    extract_numbers_from_article,
    ingest_article_numbers,
)
from number_utils import collapse_grouped_thousands, normalize_burmese_numerals
from report_formatter import clean_number_value


class _FakeModels:
    """Scripted stand-in for ``client.models``.

    ``script`` maps model name -> response text | Exception.
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(model)
        outcome = self.script.get(model, RuntimeError(f"unexpected model {model}"))
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def _fake_client(script):
    return SimpleNamespace(models=_FakeModels(script))


class CleanNumberTests(unittest.TestCase):
    """clean_number decides what gets written into newspaper_numbers.value."""

    def test_comma_thousands_are_removed(self):
        self.assertEqual(clean_number("7,150,000"), "7150000")
        self.assertEqual(clean_number("2,800"), "2800")

    def test_burmese_digits_are_translated(self):
        self.assertEqual(clean_number("၂,၈၀၀"), "2800")
        self.assertEqual(clean_number("၇,၁၅၀,၀၀၀"), "7150000")

    def test_burmese_comma_thousands_are_removed(self):
        """'၇၊၁၅၀၊၀၀၀' uses the Burmese comma as a thousands separator.

        Stripping only "," and then taking the leftmost digit run collapsed a
        7.15-million-kyat gold price to "7".
        """
        self.assertEqual(clean_number("၇၊၁၅၀၊၀၀၀"), "7150000")
        self.assertEqual(clean_number("၇၊၁၅၀၊၀၀၀ ကျပ်"), "7150000")

    def test_space_and_nbsp_thousands_are_removed(self):
        self.assertEqual(clean_number("1 500 000"), "1500000")
        self.assertEqual(clean_number("7\u00a0150\u00a0000"), "7150000")
        self.assertEqual(clean_number("7\u202f150\u202f000"), "7150000")

    def test_two_prices_separated_by_burmese_comma_are_not_merged(self):
        """Safety property: a separator before 4+ digits is a list, not grouping.

        Merging here would fuse two prices into one enormous wrong price, which
        is worse than the truncation this change removes.
        """
        self.assertEqual(clean_number("၂၅၀၀၊၂၆၀၀"), "2500")
        self.assertEqual(clean_number("2500၊2600"), "2500")

    def test_number_followed_by_a_unit_is_not_merged(self):
        self.assertEqual(clean_number("2500 ကျပ်"), "2500")
        self.assertEqual(clean_number("118 ဦး"), "118")

    def test_decimals_and_signs_survive(self):
        self.assertEqual(clean_number("1500.50"), "1500.50")
        self.assertEqual(clean_number("140.474"), "140.474")
        self.assertEqual(clean_number("-100"), "-100")

    def test_no_digits_returns_empty_not_the_raw_text(self):
        """A non-numeric answer must not land in a column read with float()."""
        self.assertEqual(clean_number("မရှိ"), "")
        self.assertEqual(clean_number(""), "")
        self.assertEqual(clean_number(None), "")

    def test_range_keeps_the_lower_bound(self):
        self.assertEqual(clean_number("၂,၅၀၀-၂,၆၀၀"), "2500")

    def test_burmese_word_decimal_is_understood(self):
        """ဒသမ is the Burmese word for "decimal point".

        All three of these forms were found in production `original_value` rows;
        plain digit translation turned them into 111, 10 and 2.
        """
        self.assertEqual(clean_number("၁၁၁ ဒသမ ၃၂"), "111.32")
        self.assertEqual(clean_number("၉၆ ဒသမ ၉၅"), "96.95")
        self.assertEqual(clean_number("၅ ဒသမ ၅ - ၇ ဒသမ ၂"), "5.5")

    def test_word_zero_before_a_decimal_is_understood(self):
        self.assertEqual(clean_number("သုည ဒသမ ၁၀"), "0.10")

    def test_letter_wa_used_as_zero_between_digits(self):
        """"၂ဝ၂၆" is 2026 written with the letter ဝ standing in for zero.

        Parsed as "2" — a 1000x error.
        """
        self.assertEqual(clean_number("၂ဝ၂၆"), "2026")
        self.assertEqual(clean_number("၂ဝ၂၇"), "2027")

    def test_letter_wa_elsewhere_in_the_text_is_left_alone(self):
        """Blanket ဝ -> 0 would turn "ဝန်ကြီး ၂၅၀၀" into "0..." and parse as 0."""
        self.assertEqual(clean_number("ဝန်ကြီး ၂၅၀၀"), "2500")
        self.assertEqual(clean_number("ဝ၉-၄၂၁၁၂၄၈၄ဝ"), "9")


class ParseExtractedJsonTests(unittest.TestCase):
    def test_array_of_objects_passes_through(self):
        self.assertEqual(_parse_extracted_json('[{"value": "3050"}]'), [{"value": "3050"}])

    def test_json_fence_is_stripped(self):
        self.assertEqual(_parse_extracted_json('```json\n[{"value": "9"}]\n```'), [{"value": "9"}])

    def test_empty_array_is_valid(self):
        self.assertEqual(_parse_extracted_json("[]"), [])

    def test_object_response_raises_so_the_fallback_chain_advances(self):
        """The load-bearing fix.

        generate_content_with_fallback only moves to the next model when parse
        RAISES. A model answering with {"prices": [...]} was accepted as a
        success and then discarded by extract_numbers_from_article, losing every
        figure for that article with no retry and no log.
        """
        with self.assertRaises(ValueError):
            _parse_extracted_json('{"prices": [{"value": "1"}]}')

    def test_list_of_scalars_raises(self):
        with self.assertRaises(ValueError):
            _parse_extracted_json('["3050", "7150000"]')

    def test_non_dict_entries_are_dropped(self):
        self.assertEqual(_parse_extracted_json('[{"value": "1"}, "junk", 7]'), [{"value": "1"}])

    def test_malformed_json_propagates(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_extracted_json('[{"value": ')


class ExtractNumbersFromArticleTests(unittest.TestCase):
    def test_returns_rows_from_the_primary_model(self):
        client = _fake_client({gemini_config.GEMINI_MODEL: '[{"value": "3050"}]'})
        rows = extract_numbers_from_article(
            article_text="Octane 92 ရည်ညွှန်းလက်ကားဈေး ၃,၀၅၀ ကျပ်",
            headline="h",
            publication_date="2026-09-18",
            section="s",
            genai_client=client,
        )
        self.assertEqual(rows, [{"value": "3050"}])
        self.assertEqual(client.models.calls, [gemini_config.GEMINI_MODEL])

    def test_object_answer_falls_through_to_the_next_model(self):
        """Before the parse fix this returned [] and the figures vanished."""
        primary, fallback = gemini_config.GEMINI_MODELS[0], gemini_config.GEMINI_MODELS[1]
        client = _fake_client(
            {primary: '{"prices": [{"value": "999"}]}', fallback: '[{"value": "3050"}]'}
        )
        rows = extract_numbers_from_article(
            article_text="ရွှေတစ်ကျပ်သား ၇,၁၅၀,၀၀၀ ကျပ်",
            headline="h",
            publication_date="2026-09-18",
            section="s",
            genai_client=client,
        )
        self.assertEqual(client.models.calls, [primary, fallback])
        self.assertEqual(rows, [{"value": "3050"}])

    def test_short_text_and_missing_client_return_empty(self):
        client = _fake_client({})
        for text in ("", "   ", "short"):
            with self.subTest(text=text):
                self.assertEqual(
                    extract_numbers_from_article(
                        article_text=text,
                        headline="h",
                        publication_date="d",
                        section="s",
                        genai_client=client,
                    ),
                    [],
                )
        self.assertEqual(
            extract_numbers_from_article(
                article_text="x" * 50,
                headline="h",
                publication_date="d",
                section="s",
                genai_client=None,
            ),
            [],
        )

    def test_every_model_failing_returns_empty_rather_than_raising(self):
        primary, fallback = gemini_config.GEMINI_MODELS[0], gemini_config.GEMINI_MODELS[1]
        client = _fake_client({primary: "not json", fallback: "also not json"})
        rows = extract_numbers_from_article(
            article_text="ရွှေဈေး ၇,၁၅၀,၀၀၀ ကျပ်",
            headline="h",
            publication_date="d",
            section="s",
            genai_client=client,
        )
        self.assertEqual(rows, [])


class _FakeTable:
    def __init__(self):
        self.rows = []

    def insert(self, rows):
        self.rows.extend(rows)
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class IngestArticleNumbersTests(unittest.TestCase):
    """Pins a function that nothing currently calls (see the module docstring)."""

    def setUp(self):
        self.table = _FakeTable()
        self.supabase = SimpleNamespace(from_=lambda name: self.table)

    def test_missing_clients_are_a_no_op(self):
        self.assertIsNone(ingest_article_numbers(None, None, "a", "d", "h", "body text here"))
        client = _fake_client({gemini_config.GEMINI_MODEL: "[]"})
        self.assertIsNone(ingest_article_numbers(None, client, "a", "d", "h", "body text here"))

    def test_rows_carry_the_article_id_and_a_cleaned_value(self):
        client = _fake_client(
            {
                gemini_config.GEMINI_MODEL: json.dumps(
                    [
                        {
                            "context": "ရွှေတစ်ကျပ်သား",
                            "value": "၇,၁၅၀,၀၀၀",
                            "original_value": "၇,၁၅၀,၀၀၀",
                            "unit": "ကျပ်",
                            "source_text": "ရွှေဈေး",
                        }
                    ]
                )
            }
        )
        ingest_article_numbers(
            self.supabase, client, "art-1", "2026-09-18", "ရွှေဈေး", "body text here"
        )
        self.assertEqual(len(self.table.rows), 1)
        self.assertEqual(self.table.rows[0]["article_id"], "art-1")
        self.assertEqual(self.table.rows[0]["value"], "7150000")

    def test_items_without_a_number_are_skipped(self):
        client = _fake_client(
            {
                gemini_config.GEMINI_MODEL: json.dumps(
                    [
                        {"context": "no figure", "value": "မရှိ"},
                        {"context": "fuel", "value": "2500"},
                    ]
                )
            }
        )
        ingest_article_numbers(self.supabase, client, "a", "d", "h", "body text here")
        self.assertEqual([r["value"] for r in self.table.rows], ["2500"])

    def test_non_dict_items_are_skipped_rather_than_crashing(self):
        """The caller indexes every item with it.get(...); a bare scalar used to
        raise AttributeError and take the whole article's figures down with it."""
        with patch(
            "auto_numeric_extractor.extract_numbers_from_article",
            return_value=["3050", {"context": "fuel", "value": "2500"}],
        ):
            ingest_article_numbers(
                self.supabase, _fake_client({}), "a", "d", "h", "body text here"
            )
        self.assertEqual([r["value"] for r in self.table.rows], ["2500"])


class NormalizeBurmeseNumeralsTests(unittest.TestCase):
    """number_utils owns digit + word-numeral normalisation for both sides."""

    def test_translates_digits(self):
        self.assertEqual(normalize_burmese_numerals("၂၅၀၀"), "2500")

    def test_resolves_the_decimal_word(self):
        self.assertEqual(normalize_burmese_numerals("၁၁၁ ဒသမ ၃၂"), "111.32")

    def test_resolves_word_zero_only_before_a_decimal(self):
        self.assertEqual(normalize_burmese_numerals("သုည ဒသမ ၁၀"), "0.10")
        # Not a number phrase — left alone.
        self.assertEqual(normalize_burmese_numerals("သုည နှင့် ၅"), "သုည နှင့် 5")

    def test_resolves_letter_wa_only_between_digits(self):
        self.assertEqual(normalize_burmese_numerals("၂ဝ၂၆"), "2026")
        self.assertEqual(normalize_burmese_numerals("ဝန်ကြီး"), "ဝန်ကြီး")

    def test_handles_empty_input(self):
        self.assertEqual(normalize_burmese_numerals(""), "")
        self.assertEqual(normalize_burmese_numerals(None), "")


class CollapseGroupedThousandsTests(unittest.TestCase):
    """number_utils is the single source of truth for both sides of the pipeline."""

    def test_collapses_grouped_runs(self):
        self.assertEqual(collapse_grouped_thousands("7၊150၊000"), "7150000")
        self.assertEqual(collapse_grouped_thousands("1 500 000"), "1500000")
        self.assertEqual(collapse_grouped_thousands("7\u00a0150\u00a0000"), "7150000")

    def test_leaves_ungrouped_text_untouched(self):
        self.assertEqual(collapse_grouped_thousands("2500 ကျပ်"), "2500 ကျပ်")
        self.assertEqual(collapse_grouped_thousands("2500 300"), "2500 300")
        self.assertEqual(collapse_grouped_thousands("၂၅၀၀၊၂၆၀၀"), "၂၅၀၀၊၂၆၀၀")
        self.assertEqual(collapse_grouped_thousands("1500.50"), "1500.50")

    def test_collapses_only_the_grouped_part_of_a_longer_string(self):
        self.assertEqual(collapse_grouped_thousands("7၊150၊000 ကျပ်"), "7150000 ကျပ်")

    def test_it_removes_separators_but_does_not_translate_digits(self):
        """Callers normalize digits first; this helper only removes separators.

        ``\\d`` matches Burmese digits too (they are Unicode Nd), so the run is
        recognised — but the digits themselves are left as-is. That is why both
        callers translate before calling, and why float() works downstream.
        """
        self.assertEqual(collapse_grouped_thousands("၇၊၁၅၀၊၀၀၀"), "၇၁၅၀၀၀၀")

    def test_handles_empty_input(self):
        self.assertEqual(collapse_grouped_thousands(""), "")
        self.assertEqual(collapse_grouped_thousands(None), "")


class WriteReadAgreementTests(unittest.TestCase):
    """The writer stores ``value``; the reader parses it back.

    Two functions, two modules, and they previously drifted — both stripped only
    "," and took the leftmost digit run. Anything the writer accepts must come
    back at the same magnitude on the read side.
    """

    CORPUS = (
        "၇၊၁၅၀၊၀၀၀",
        "၇၊၁၅၀၊၀၀၀ ကျပ်",
        "7,150,000",
        "1 500 000",
        "7\u00a0150\u00a0000",
        "2,800",
        "၂,၈၀၀",
        "-100",
        "2500 ကျပ်",
        "၂၅၀၀၊၂၆၀၀",
        "2500 300",
        "၁၁၁ ဒသမ ၃၂",
        "သုည ဒသမ ၁၀",
        "၂ဝ၂၆",
        "ဝန်ကြီး ၂၅၀၀",
    )

    def test_reader_agrees_with_writer_on_every_sample(self):
        for sample in self.CORPUS:
            with self.subTest(sample=sample):
                written = clean_number(sample)
                self.assertNotEqual(written, "", f"{sample!r} produced no value")
                self.assertEqual(float(written), clean_number_value(sample))


if __name__ == "__main__":
    unittest.main()
