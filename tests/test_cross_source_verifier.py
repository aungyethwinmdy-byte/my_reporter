"""Tests for cross_source_verifier — the /compare fact-check engine.

This module had no coverage at all, which is how the `/compare` NameError in
telegram_bot stayed hidden. Everything here is offline: Supabase and Gemini are
replaced with fakes, so no keys and no network are required.

Covered:
  * sanitize_keyword / safe_json_extract (input hardening for SQL-ish injection)
  * build_state_context / build_independent_context (citation + truncation)
  * run_cross_source_comparison failure paths (no client, no keywords, no hits)
  * run_cross_source_comparison success path (report + metrics footer)
"""

import unittest
from unittest.mock import MagicMock, patch

import cross_source_verifier as csv_mod


class SanitizeKeywordTests(unittest.TestCase):
    """Keywords are interpolated into PostgREST `.or_()` filters — strip metachars."""

    def test_strips_postgrest_metacharacters(self):
        self.assertEqual(csv_mod.sanitize_keyword("octane,price"), "octaneprice")
        self.assertEqual(csv_mod.sanitize_keyword("a(b)c"), "abc")
        self.assertEqual(csv_mod.sanitize_keyword('say "hi"'), "say hi")
        self.assertEqual(csv_mod.sanitize_keyword("100% *wild*"), "100 wild")
        self.assertEqual(csv_mod.sanitize_keyword("back\\slash"), "backslash")

    def test_trims_whitespace_and_handles_empty(self):
        self.assertEqual(csv_mod.sanitize_keyword("  မြဝတီ  "), "မြဝတီ")
        self.assertEqual(csv_mod.sanitize_keyword(""), "")
        self.assertEqual(csv_mod.sanitize_keyword(",,,()"), "")

    def test_burmese_text_is_preserved(self):
        keyword = "နယ်စပ်ဂိတ်"
        self.assertEqual(csv_mod.sanitize_keyword(keyword), keyword)


class SafeJsonExtractTests(unittest.TestCase):
    def test_parses_clean_json_array(self):
        self.assertEqual(csv_mod.safe_json_extract('["a", "b"]'), ["a", "b"])

    def test_extracts_array_from_surrounding_prose(self):
        text = 'Here you go:\n["မြဝတီ", "ကုန်သွယ်ရေး"]\nHope that helps.'
        self.assertEqual(csv_mod.safe_json_extract(text), ["မြဝတီ", "ကုန်သွယ်ရေး"])

    def test_returns_empty_list_on_garbage(self):
        self.assertEqual(csv_mod.safe_json_extract("not json at all"), [])
        self.assertEqual(csv_mod.safe_json_extract(""), [])

    def test_array_followed_by_prose_containing_brackets(self):
        """A greedy `\\[.*\\]` spanned to the LAST bracket and lost the array.

        This is the failure that silently downgraded /compare to the
        whitespace keyword fallback.
        """
        text = '["မြဝတီ", "ကုန်သွယ်ရေး"]\nNote: [all keywords are in Myanmar script]'
        self.assertEqual(csv_mod.safe_json_extract(text), ["မြဝတီ", "ကုန်သွယ်ရေး"])

    def test_two_arrays_returns_the_first(self):
        text = '["မြဝတီ"]\n["ကုန်သွယ်ရေး"]'
        self.assertEqual(csv_mod.safe_json_extract(text), ["မြဝတီ"])

    def test_nested_array_still_parses(self):
        """Greedy is tried first precisely so a nested array is not split."""
        self.assertEqual(csv_mod.safe_json_extract('[["a"], ["b"]]'), [["a"], ["b"]])

    def test_bracketed_prose_before_the_array_is_skipped(self):
        text = 'Thinking: [step 1] then [step 2]\n["နယ်စပ်ဂိတ်", "ကုန်သွယ်ရေး"]'
        self.assertEqual(
            csv_mod.safe_json_extract(text), ["နယ်စပ်ဂိတ်", "ကုန်သွယ်ရေး"]
        )


class ContextBuilderTests(unittest.TestCase):
    STATE_ARTICLES = [
        {
            "newspaper_name": "မြန်မာ့အလင်း",
            "issue_date": "2026-09-17",
            "page_no": 1,
            "headline": "ခေါင်းစဉ် တစ်",
            "body_text": "အကြောင်းအရာ " * 10,
        }
    ]
    INDEPENDENT_ARTICLES = [
        {
            "source_name": "BBC Burmese",
            "published_date": "2026-09-17",
            "url": "https://example.com/a",
            "headline": "Headline",
            "body_text": "body " * 10,
        }
    ]

    def test_state_context_includes_citation(self):
        ctx = csv_mod.build_state_context(self.STATE_ARTICLES)
        self.assertIn("Source: မြန်မာ့အလင်း", ctx)
        self.assertIn("2026-09-17", ctx)
        self.assertIn("Page 1", ctx)
        self.assertIn("ခေါင်းစဉ် တစ်", ctx)

    def test_state_context_handles_empty_list(self):
        ctx = csv_mod.build_state_context([])
        self.assertIn("STATE MEDIA", ctx)
        self.assertIn("မတွေ့ရှိပါ", ctx)

    def test_independent_context_includes_link(self):
        ctx = csv_mod.build_independent_context(self.INDEPENDENT_ARTICLES)
        self.assertIn("Source: BBC Burmese", ctx)
        self.assertIn("https://example.com/a", ctx)

    def test_independent_context_handles_empty_list(self):
        ctx = csv_mod.build_independent_context([])
        self.assertIn("INDEPENDENT MEDIA", ctx)

    def test_body_text_is_truncated_to_budget(self):
        big = [dict(self.STATE_ARTICLES[0], body_text="က" * 9000)]
        ctx = csv_mod.build_state_context(big, max_chars=100)
        self.assertLess(len(ctx), 1000, "article bodies must be truncated")

    def test_missing_fields_do_not_raise(self):
        # Rows missing headline/body must degrade, not crash.
        ctx = csv_mod.build_state_context([{"newspaper_name": "ကြေးမုံ"}])
        self.assertIn("ကြေးမုံ", ctx)
        ctx = csv_mod.build_independent_context([{}])
        self.assertIn("Independent Media", ctx)


class RunComparisonFailureTests(unittest.TestCase):
    """Guards for the paths the /compare handler relies on."""

    def test_returns_failure_when_gemini_missing(self):
        with patch.object(csv_mod, "genai_client", None):
            result = csv_mod.run_cross_source_comparison("မြဝတီ")
        self.assertFalse(result["success"])
        self.assertEqual(result["state_count"], 0)
        self.assertEqual(result["independent_count"], 0)
        self.assertIn("Gemini", result["report"])

    def test_returns_failure_when_no_keywords(self):
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "plan_comparison_keywords", return_value=[]
        ):
            result = csv_mod.run_cross_source_comparison("x")
        self.assertFalse(result["success"])
        self.assertEqual(result["keywords"], [])

    def test_returns_failure_when_both_sources_empty(self):
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "plan_comparison_keywords", return_value=["kw"]
        ), patch.object(
            csv_mod, "fetch_state_media_articles", return_value=[]
        ), patch.object(
            csv_mod, "fetch_independent_media_articles", return_value=[]
        ):
            result = csv_mod.run_cross_source_comparison("မြဝတီ")
        self.assertFalse(result["success"])
        self.assertIn("မတွေ့ရှိပါ", result["report"])
        self.assertEqual(result["keywords"], ["kw"])


class RunComparisonSuccessTests(unittest.TestCase):
    def test_success_path_returns_report_and_metrics(self):
        state = [
            {
                "newspaper_name": "မြန်မာ့အလင်း",
                "issue_date": "2026-09-17",
                "page_no": 1,
                "headline": "H",
                "body_text": "B",
            }
        ]
        indep = [
            {
                "source_name": "BBC Burmese",
                "published_date": "2026-09-17",
                "url": "https://example.com/a",
                "headline": "H2",
                "body_text": "B2",
            }
        ]
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "plan_comparison_keywords", return_value=["မြဝတီ"]
        ), patch.object(
            csv_mod, "fetch_state_media_articles", return_value=state
        ), patch.object(
            csv_mod, "fetch_independent_media_articles", return_value=indep
        ), patch.object(
            csv_mod, "generate_verification_report", return_value="REPORT BODY"
        ):
            result = csv_mod.run_cross_source_comparison("မြဝတီ")

        self.assertTrue(result["success"])
        self.assertEqual(result["state_count"], 1)
        self.assertEqual(result["independent_count"], 1)
        self.assertEqual(result["keywords"], ["မြဝတီ"])
        self.assertIn("REPORT BODY", result["report"])
        # Metrics footer must carry the counts and the model chain.
        self.assertIn("State Media Articles: 1", result["report"])
        self.assertIn("Independent Articles: 1", result["report"])
        self.assertIn("Model:", result["report"])

    def test_report_is_generated_when_only_one_side_has_hits(self):
        state = [
            {
                "newspaper_name": "ကြေးမုံ",
                "issue_date": "2026-09-17",
                "page_no": 2,
                "headline": "H",
                "body_text": "B",
            }
        ]
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "plan_comparison_keywords", return_value=["kw"]
        ), patch.object(
            csv_mod, "fetch_state_media_articles", return_value=state
        ), patch.object(
            csv_mod, "fetch_independent_media_articles", return_value=[]
        ), patch.object(
            csv_mod, "generate_verification_report", return_value="PARTIAL"
        ):
            result = csv_mod.run_cross_source_comparison("kw")

        self.assertTrue(result["success"], "one-sided results are still reportable")
        self.assertEqual(result["state_count"], 1)
        self.assertEqual(result["independent_count"], 0)


class KeywordPlannerContractTests(unittest.TestCase):
    """plan_comparison_keywords must ALWAYS return a list of clean strings.

    `safe_json_extract` passes a JSON *object* through unchanged (it is valid
    JSON), so the planner's `isinstance(parsed, list)` guard is load-bearing:
    without it a dict would be iterated as keys. These tests pin that contract.
    """

    def test_object_response_falls_back_to_topic_words(self):
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "generate_with_retry", return_value='{"keywords": ["a"]}'
        ):
            keywords = csv_mod.plan_comparison_keywords("မြဝတီ ကုန်သွယ်ရေး")
        self.assertIsInstance(keywords, list)
        self.assertTrue(all(isinstance(k, str) for k in keywords))
        self.assertIn("မြဝတီ", keywords)

    def test_no_gemini_client_returns_topic_words_without_metachars(self):
        with patch.object(csv_mod, "genai_client", None):
            keywords = csv_mod.plan_comparison_keywords("မြဝတီ, ကုန်သွယ်ရေး)")
        self.assertIsInstance(keywords, list)
        for kw in keywords:
            self.assertNotIn(",", kw)
            self.assertNotIn(")", kw)

    def test_api_failure_returns_fallback_keywords(self):
        with patch.object(csv_mod, "genai_client", MagicMock()), patch.object(
            csv_mod, "generate_with_retry", side_effect=RuntimeError("boom")
        ):
            keywords = csv_mod.plan_comparison_keywords("မြဝတီ ကုန်သွယ်ရေး")
        self.assertIsInstance(keywords, list)
        self.assertTrue(keywords, "must fall back to topic words, never empty")


if __name__ == "__main__":
    unittest.main()
