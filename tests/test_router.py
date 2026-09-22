"""Unit tests for system router, multi-route dispatcher, and ingestion pipeline.

Covers greeting/meta detection, sources count, system status, general conversation
fallback chain, numeric ingestion flow with Burmese digit normalization, page-2-first
search order, and scanned-PDF native vision fallback.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gemini_config
import ingest_engine as ie
from ingest_engine import extract_numbers_into_db, process_and_ingest_pdf
from system_router import (
    get_greeting_response,
    get_sources_report,
    get_system_status,
    handle_general_ai_conversation,
    is_greeting_or_casual,
    is_system_meta_query,
)
from telegram_bot import execute_articles_search


class RouterClassificationTests(unittest.TestCase):
    def test_greeting_detection_positive_and_negative(self):
        self.assertTrue(is_greeting_or_casual("မင်္ဂလာပါ"))
        self.assertTrue(is_greeting_or_casual("hello"))
        self.assertTrue(is_greeting_or_casual("ကျေးဇူးတင်ပါတယ်"))
        self.assertFalse(is_greeting_or_casual("စက်သုံးဆီ ဈေးနှုန်း ဘယ်လောက်လဲ"))
        self.assertFalse(is_greeting_or_casual(""))

    def test_greeting_response_guidance(self):
        resp = get_greeting_response()
        self.assertIn("Myanmar Intelligent Newsroom", resp)
        self.assertIn("/compare", resp)
        self.assertIn("/sources", resp)

    def test_system_meta_query_detection(self):
        self.assertTrue(is_system_meta_query("သတင်းဌာန ဘယ်နှစ်ခုလဲ"))
        self.assertTrue(is_system_meta_query("ဘယ်မီဒီယာတွေ ပါလဲ"))
        self.assertTrue(is_system_meta_query("စနစ်အခြေအနေ status"))
        self.assertFalse(is_system_meta_query("တောင်ငူ ရေကြီးမှု သတင်း"))
        self.assertFalse(is_system_meta_query(""))

    def test_news_questions_mentioning_media_are_not_system_queries(self):
        """ROUTE 2 runs before ROUTE 4, so a false positive here costs the answer.

        "မီဒီယာ" (media) and "သတင်းဌာန" (news agency) are ordinary news
        vocabulary. Matching them on their own replaced these questions with the
        canned source list, so they never reached the article search.
        """
        for query in (
            "မီဒီယာတွေအပေါ် ဖိအားပေးမှုသတင်း",
            "နိုင်ငံခြားသတင်းဌာနတွေ ဘာပြောလဲ",
            "မီဒီယာလွတ်လပ်ခွင့် အခြေအနေ",
            "သတင်းဌာနတွေမှာ ဖော်ပြထားတဲ့ ရေကြီးမှုသတင်း",
        ):
            self.assertFalse(is_system_meta_query(query), query)

    def test_media_word_with_a_counting_form_is_a_system_query(self):
        self.assertTrue(is_system_meta_query("သတင်းဌာန စာရင်း"))
        self.assertTrue(is_system_meta_query("source ဘယ်နှခုရှိလဲ"))

    def test_greeting_followed_by_a_question_is_not_a_greeting(self):
        """"hi ရွှေဈေး" used to be answered with a hello.

        The old rule was `startswith(greeting) and len(query) <= 15`, so a short
        greeting prefixed onto a short question swallowed the question entirely.
        """
        for query in (
            "hi ရွှေဈေး",
            "hello စက်သုံးဆီဈေး",
            "thanks ဒီနေ့သတင်း",
            "မင်္ဂလာပါ ရေကြီးမှုသတင်း",
            "မင်္ဂလာရက်",  # a holiday name, not a greeting
        ):
            self.assertFalse(is_greeting_or_casual(query), query)

    def test_greeting_with_politeness_still_matches(self):
        for query in (
            "မင်္ဂလာပါ", "မင်္ဂလာပါခင်ဗျာ", "hello", "hi", "hi there",
            "hello everyone", "good morning", "နေကောင်းလား", "ကျေးဇူးတင်ပါတယ်",
            "မင်္ဂလာပါ!",
        ):
            self.assertTrue(is_greeting_or_casual(query), query)

    def test_sources_report_total_and_breakdown(self):
        report = get_sources_report()
        self.assertIn("(6)", report)
        self.assertIn("မြန်မာ့အလင်း သတင်းစာ", report)
        self.assertIn("ကြေးမုံ သတင်းစာ", report)
        self.assertIn("BBC Burmese", report)
        self.assertIn("The Irrawaddy", report)
        self.assertIn("RFA Burmese", report)
        self.assertIn("PPIB", report)


class SystemStatusTests(unittest.TestCase):
    def test_system_status_with_none_client(self):
        status = get_system_status(None)
        self.assertIn("System Status", status)
        self.assertIn("0 ပုဒ်", status)
        self.assertIn("0 ခု", status)
        self.assertIn("မသိရှိပါ", status)

    def test_system_status_with_broken_client(self):
        broken = MagicMock()
        broken.from_.side_effect = RuntimeError("database disconnected")
        status = get_system_status(broken)
        self.assertIn("System Status", status)
        self.assertIn("0 ပုဒ်", status)
        self.assertIn("0 ခု", status)

    def test_system_status_with_valid_client(self):
        client = MagicMock()
        art_res = MagicMock(count=120, data=[])
        num_res = MagicMock(count=45, data=[])
        date_res = MagicMock(data=[{"issue_date": "2026-09-17"}])

        def fake_from(table_name):
            builder = MagicMock()
            if table_name == "articles":
                def fake_select(cols, count=None):
                    sel_mock = MagicMock()
                    if count == "exact":
                        sel_mock.limit.return_value.execute.return_value = art_res
                    else:
                        sel_mock.order.return_value.limit.return_value.execute.return_value = date_res
                    return sel_mock
                builder.select = fake_select
            elif table_name == "newspaper_numbers":
                sel_mock = MagicMock()
                sel_mock.limit.return_value.execute.return_value = num_res
                builder.select.return_value = sel_mock
            return builder

        client.from_ = fake_from
        status = get_system_status(client)
        self.assertIn("120 ပုဒ်", status)
        self.assertIn("45 ခု", status)
        self.assertIn("2026-09-17", status)


class GeneralConversationTests(unittest.TestCase):
    def test_general_ai_conversation_with_none_client(self):
        answer = handle_general_ai_conversation("မင်္ဂလာပါ", None)
        self.assertIn("မြန်မာ့သတင်းစောင့်ကြည့်ရေး", answer)

    def test_general_ai_conversation_fallback_chain(self):
        calls = []

        class FakeModels:
            def generate_content(self, *, model, contents, config=None):
                calls.append(model)
                if model == gemini_config.GEMINI_MODELS[0]:
                    raise RuntimeError("429 Resource Exhausted")
                return SimpleNamespace(text="ဒါက ဒုတိယ model အဖြေဖြစ်ပါတယ်။")

        fake_client = SimpleNamespace(models=FakeModels())
        ans = handle_general_ai_conversation("ဘာလုပ်ပေးနိုင်လဲ", fake_client)
        self.assertEqual(ans, "ဒါက ဒုတိယ model အဖြေဖြစ်ပါတယ်။")
        self.assertEqual(calls, [gemini_config.GEMINI_MODELS[0], gemini_config.GEMINI_MODELS[1]])


class IngestAndSearchTests(unittest.TestCase):
    @patch("ingest_engine.supabase")
    @patch("ingest_engine.gemini_client")
    def test_ingest_numeric_flow_normalizes_burmese_digits(self, mock_gemini, mock_supabase):
        extracted = [
            {
                "context": "Octane 92",
                "value": "၃,၀၅၀",
                "original_value": "၃,၀၅၀",
                "unit": "ကျပ်",
                "source_text": "Octane 92 တစ်လီတာ ၃,၀၅၀ ကျပ်",
            }
        ]
        inserted_rows = []
        mock_table = MagicMock()
        mock_table.insert.side_effect = lambda rows: MagicMock(execute=lambda: inserted_rows.extend(rows))
        mock_supabase.from_.return_value = mock_table

        with patch("ingest_engine.extract_numbers_from_article", return_value=extracted):
            extract_numbers_into_db(
                headline="စက်သုံးဆီ ဈေးနှုန်း ထုတ်ပြန်",
                body_text="Octane 92 တစ်လီတာ ၃,၀၅၀ ကျပ်ဖြင့် ရောင်းချလျက်ရှိသည်။",
                pub_date="2026-09-17",
                section="စက်သုံးဆီ",
            )

        self.assertEqual(len(inserted_rows), 1)
        self.assertEqual(inserted_rows[0]["value"], "3050")
        self.assertEqual(inserted_rows[0]["original_value"], "၃,၀၅၀")
        self.assertEqual(inserted_rows[0]["context"], "Octane 92")

    @patch("ingest_engine.supabase")
    @patch("ingest_engine.gemini_client")
    def test_ingest_numeric_flow_skips_when_no_digits(self, mock_gemini, mock_supabase):
        with patch("ingest_engine.extract_numbers_from_article") as mock_extract:
            extract_numbers_into_db(
                headline="ခေါင်းစဉ်",
                body_text="ဂဏန်းလုံးဝမပါသော သာမန်စာသားဖြစ်ပါသည်။",
                pub_date="2026-09-17",
            )
            mock_extract.assert_not_called()

    def test_execute_articles_search_page_2_first_order(self):
        mock_supabase = MagicMock()
        p2_record = {
            "article_id": "art-p2",
            "newspaper_name": "မြန်မာ့အလင်း",
            "issue_date": "2026-09-17",
            "page_no": 2,
            "headline": "Page 2 News",
            "body_text": "body",
        }
        kw_record = {
            "article_id": "art-kw",
            "newspaper_name": "မြန်မာ့အလင်း",
            "issue_date": "2026-09-17",
            "page_no": 5,
            "headline": "Octane News",
            "body_text": "body",
        }

        def fake_from(table):
            builder = MagicMock()
            def fake_select(*cols):
                sel = MagicMock()
                def fake_eq(col, val):
                    eq_mock = MagicMock()
                    if col == "page_no" and val == 2:
                        eq_mock.limit.return_value.execute.return_value = MagicMock(data=[p2_record])
                    else:
                        eq_mock.ilike.return_value.eq.side_effect = fake_eq
                        eq_mock.ilike.return_value.or_.return_value.limit.return_value.execute.return_value = MagicMock(data=[kw_record])
                    return eq_mock
                sel.eq.side_effect = fake_eq
                return sel
            builder.select = fake_select
            return builder

        mock_supabase.from_ = fake_from
        results = execute_articles_search(mock_supabase, ["Octane"], ["2026-09-17"])
        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0]["article_id"], "art-p2")
        self.assertEqual(results[0]["page_no"], 2)
        self.assertEqual(results[1]["article_id"], "art-kw")

    def test_execute_articles_search_sanitizes_keywords(self):
        mock_supabase = MagicMock()
        captured_or = []

        def fake_from(table):
            builder = MagicMock()
            sel = MagicMock()
            eq1 = MagicMock()
            ilike1 = MagicMock()

            builder.select.return_value = sel
            sel.eq.return_value = eq1
            eq1.ilike.return_value = ilike1
            ilike1.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])

            def fake_or(clause):
                captured_or.append(clause)
                m = MagicMock()
                m.limit.return_value.execute.return_value = MagicMock(data=[])
                return m

            ilike1.or_ = fake_or
            return builder

        mock_supabase.from_ = fake_from
        execute_articles_search(mock_supabase, ["octane;", "price'--"], ["2026-09-17"])
        for clause in captured_or:
            self.assertNotIn(";", clause)
            self.assertNotIn("'", clause)

    @patch("ingest_engine.supabase")
    @patch("ingest_engine.extract_text_from_pdf", return_value=[])
    @patch("ingest_engine.extract_page2_tables", return_value=[])
    @patch("ingest_engine.parse_articles_with_gemini_native_pdf")
    @patch("ingest_engine.insert_article_to_supabase", return_value=True)
    def test_process_and_ingest_pdf_scanned_pdf_native_vision_fallback(
        self, mock_insert, mock_native_pdf, mock_p2, mock_extract_text, mock_supabase
    ):
        mock_native_pdf.return_value = [
            {"headline": "Scanned Vision Article", "body_text": "Extracted with Gemini Native Vision", "page_no": 1}
        ]
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            ok = process_and_ingest_pdf(tmp.name, "မြန်မာ့အလင်း", "2026-09-17")
            self.assertTrue(ok)
            mock_native_pdf.assert_called_once_with(tmp.name, "မြန်မာ့အလင်း", "2026-09-17")
            mock_insert.assert_called_once()
            record = mock_insert.call_args[0][0]
            self.assertEqual(record["headline"], "Scanned Vision Article")


class VisionFallbackSelectionTests(unittest.TestCase):
    """A partially-scanned PDF must still OCR the image-only pages.

    ``extract_text_from_pdf`` returns a ``(page_no, text)`` tuple for *every*
    page, with an empty string for pages that have no text layer. The old
    dispatch asked only ``any(page has text)`` and then processed the whole
    document in text mode, so a newspaper where page 1 was digital and page 2
    was a scan silently dropped page 2 — which is where the fuel and gold price
    tables usually sit.
    """

    def _run(self, pages):
        native_articles = [{"headline": "OCR ခေါင်းစဉ်", "body_text": "body", "page_no": 2}]
        with patch.object(ie, "extract_page2_tables", return_value=[]), patch.object(
            ie, "extract_text_from_pdf", return_value=pages
        ), patch.object(
            ie, "parse_articles_with_gemini_native_pdf", return_value=native_articles
        ) as native, patch.object(
            ie, "parse_articles_with_gemini_text", return_value=[]
        ), patch.object(
            ie, "insert_article_to_supabase", return_value=True
        ), patch.object(
            ie.os.path, "exists", return_value=True
        ), patch.object(
            ie, "supabase", object()
        ):
            return ie.process_and_ingest_pdf("x.pdf", "ကြေးမုံ", "2026-09-18"), native

    def test_partially_scanned_pdf_triggers_vision(self):
        ok, native = self._run([(1, "real text"), (2, ""), (3, "")])
        self.assertTrue(native.called, "image-only pages must be OCR'd")
        self.assertTrue(ok)

    def test_fully_scanned_pdf_triggers_vision(self):
        ok, native = self._run([(1, ""), (2, "")])
        self.assertTrue(native.called)
        self.assertTrue(ok)

    def test_no_pages_extracted_triggers_vision(self):
        ok, native = self._run([])
        self.assertTrue(native.called)
        self.assertTrue(ok)

    def test_fully_digital_pdf_skips_vision(self):
        """No blank pages means no OCR call — that would be wasted tokens."""
        _, native = self._run([(1, "text"), (2, "text")])
        self.assertFalse(native.called)


class VisionPassIsScopedToBlankPagesTests(unittest.TestCase):
    """The vision call must see only the pages that produced no text.

    It used to be handed the whole PDF, so for a partially-scanned issue the
    model re-extracted the text pages too — and those articles had already been
    inserted by the text pass, so each one landed twice:

        rows inserted: [(1, 'စက်သုံးဆီဈေးနှုန်း'), (1, 'စက်သုံးဆီဈေးနှုန်း')]
    """

    def _run(self, pages, native_articles, text_articles=None):
        inserted = []
        seen_paths = []

        def record_insert(rec):
            inserted.append(rec["headline"])
            return True

        def record_vision(path, newspaper, issue_date):
            seen_paths.append(path)
            return native_articles

        with patch.object(ie, "extract_page2_tables", return_value=[]), patch.object(
            ie, "extract_text_from_pdf", return_value=pages
        ), patch.object(
            ie, "parse_articles_with_gemini_native_pdf", side_effect=record_vision
        ), patch.object(
            ie, "parse_articles_with_gemini_text", return_value=text_articles or []
        ), patch.object(
            ie, "insert_article_to_supabase", side_effect=record_insert
        ), patch.object(
            ie.os.path, "exists", return_value=True
        ), patch.object(
            ie, "supabase", object()
        ):
            ok = ie.process_and_ingest_pdf("x.pdf", "ကြေးမုံ", "2026-09-18")
        return ok, inserted, seen_paths

    def test_text_page_article_is_not_inserted_twice(self):
        """The headline the text pass stored must not come back from vision."""
        article = [{"headline": "စက်သုံးဆီဈေးနှုန်း", "body_text": "body", "page_no": 1}]
        _, inserted, _ = self._run(
            [(1, "text layer"), (2, "")], native_articles=article, text_articles=article
        )
        self.assertEqual(inserted, ["စက်သုံးဆီဈေးနှုန်း"])

    def test_vision_still_adds_pages_the_text_pass_missed(self):
        _, inserted, _ = self._run(
            [(1, "text layer"), (2, "")],
            native_articles=[{"headline": "OCR ခေါင်းစဉ်", "body_text": "b", "page_no": 2}],
            text_articles=[{"headline": "စာသားခေါင်းစဉ်", "body_text": "b", "page_no": 1}],
        )
        self.assertEqual(inserted, ["စာသားခေါင်းစဉ်", "OCR ခေါင်းစဉ်"])

    def test_vision_receives_a_subset_not_the_original_file(self):
        """Guards the call site, not just the helper.

        Needs a real multi-page PDF on disk, because the subset builder falls
        back to the original file when it cannot read the source — which is the
        behaviour the other tests in this class rely on with their fake path.
        """
        from pypdf import PdfWriter

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=200, height=200)
        source = os.path.join(tmp.name, "issue.pdf")
        with open(source, "wb") as handle:
            writer.write(handle)

        seen = []
        with patch.object(ie, "extract_page2_tables", return_value=[]), patch.object(
            ie, "extract_text_from_pdf", return_value=[(1, "text"), (2, ""), (3, "")]
        ), patch.object(
            ie, "parse_articles_with_gemini_text", return_value=[]
        ), patch.object(
            ie,
            "parse_articles_with_gemini_native_pdf",
            side_effect=lambda path, newspaper, issue_date: seen.append(path) or [],
        ), patch.object(
            ie, "insert_article_to_supabase", return_value=True
        ), patch.object(
            ie, "supabase", object()
        ):
            ie.process_and_ingest_pdf(source, "ကြေးမုံ", "2026-09-18")

        self.assertEqual(len(seen), 1)
        self.assertNotEqual(
            seen[0], source, "vision must not be handed the whole document"
        )
        self.assertFalse(
            os.path.exists(seen[0]), "the temporary subset must be cleaned up"
        )

    def test_page2_table_headline_is_also_deduplicated(self):
        table = [{"headline": "ရည်ညွှန်းလက်ကားဈေးနှုန်းများ", "body_text": "b", "section": "စက်သုံးဆီ"}]
        inserted = []
        with patch.object(ie, "extract_page2_tables", return_value=table), patch.object(
            ie, "extract_text_from_pdf", return_value=[(1, "text"), (2, "")]
        ), patch.object(
            ie,
            "parse_articles_with_gemini_native_pdf",
            return_value=[{"headline": "ရည်ညွှန်းလက်ကားဈေးနှုန်းများ", "body_text": "b", "page_no": 2}],
        ), patch.object(
            ie, "parse_articles_with_gemini_text", return_value=[]
        ), patch.object(
            ie, "extract_numbers_into_db"
        ), patch.object(
            ie, "insert_article_to_supabase", side_effect=lambda rec: inserted.append(rec["headline"]) or True
        ), patch.object(
            ie.os.path, "exists", return_value=True
        ), patch.object(
            ie, "supabase", object()
        ):
            ie.process_and_ingest_pdf("x.pdf", "ကြေးမုံ", "2026-09-18")
        self.assertEqual(inserted, ["ရည်ညွှန်းလက်ကားဈေးနှုန်းများ"])


class BuildPageSubsetPdfTests(unittest.TestCase):
    def _make_pdf(self, pages):
        from pypdf import PdfWriter

        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=200, height=200)
        path = os.path.join(self.tmp.name, f"{pages}p.pdf")
        with open(path, "wb") as handle:
            writer.write(handle)
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_keeps_only_the_requested_pages(self):
        from pypdf import PdfReader

        source = self._make_pdf(5)
        subset = ie.build_page_subset_pdf(source, [2, 4])
        self.addCleanup(lambda: os.path.exists(subset) and os.remove(subset))
        self.assertIsNotNone(subset)
        self.assertEqual(len(PdfReader(subset).pages), 2)

    def test_out_of_range_pages_are_ignored(self):
        source = self._make_pdf(2)
        subset = ie.build_page_subset_pdf(source, [1, 99])
        self.addCleanup(lambda: os.path.exists(subset) and os.remove(subset))
        self.assertIsNotNone(subset)
        from pypdf import PdfReader

        self.assertEqual(len(PdfReader(subset).pages), 1)

    def test_no_pages_returns_none(self):
        self.assertIsNone(ie.build_page_subset_pdf(self._make_pdf(2), []))

    def test_unreadable_file_returns_none_instead_of_raising(self):
        self.assertIsNone(ie.build_page_subset_pdf("does-not-exist.pdf", [1]))


if __name__ == "__main__":
    unittest.main()
