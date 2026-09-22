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
    def test_rows_without_a_number_are_not_inserted(self, mock_gemini, mock_supabase):
        """Same policy as auto_numeric_extractor: no digits, no row.

        This is the third writer of `newspaper_numbers.value` and it was the
        only one that stored a valueless row. The dashboard parses that column
        as a number, so a text value there is a broken row rather than a usable
        fallback. A live sample had two rows with value='C' from the older
        echo-the-input behaviour; the current code can no longer produce them.
        """
        extracted = [
            {"context": "ဇယားကွက်", "value": "C", "original_value": "C"},
            {"context": "Octane 92", "value": "၃,၀၅၀", "original_value": "၃,၀၅၀"},
        ]
        inserted_rows = []
        mock_table = MagicMock()
        mock_table.insert.side_effect = lambda rows: MagicMock(
            execute=lambda: inserted_rows.extend(rows)
        )
        mock_supabase.from_.return_value = mock_table

        with patch("ingest_engine.extract_numbers_from_article", return_value=extracted):
            extract_numbers_into_db(
                headline="စက်သုံးဆီ ဈေးနှုန်း ထုတ်ပြန်",
                body_text="Octane 92 တစ်လီတာ ၃,၀၅၀ ကျပ် ရောင်းချလျက်ရှိသည်။",
                pub_date="2026-09-17",
                section="စက်သုံးဆီ",
            )

        self.assertEqual([row["value"] for row in inserted_rows], ["3050"])

    @patch("ingest_engine.supabase")
    @patch("ingest_engine.gemini_client")
    def test_a_wholly_non_numeric_extraction_inserts_nothing(
        self, mock_gemini, mock_supabase
    ):
        mock_table = MagicMock()
        mock_supabase.from_.return_value = mock_table

        with patch(
            "ingest_engine.extract_numbers_from_article",
            return_value=[{"value": "C", "original_value": "C"}],
        ):
            extract_numbers_into_db(
                headline="ဈေးနှုန်း",
                body_text="ဇယားကွက် C ကို 1234 ဟု ဖော်ပြထားသည်။",
                pub_date="2026-09-17",
            )

        mock_table.insert.assert_not_called()

    @patch("ingest_engine.supabase")
    @patch("ingest_engine.gemini_client")
    def test_non_dict_extraction_entries_are_skipped(self, mock_gemini, mock_supabase):
        """A model that returns bare scalars must not abort the whole insert."""
        inserted_rows = []
        mock_table = MagicMock()
        mock_table.insert.side_effect = lambda rows: MagicMock(
            execute=lambda: inserted_rows.extend(rows)
        )
        mock_supabase.from_.return_value = mock_table

        with patch(
            "ingest_engine.extract_numbers_from_article",
            return_value=["oops", {"value": "3050"}],
        ):
            extract_numbers_into_db(
                headline="ဈေးနှုန်း",
                body_text="အောက်တိန်း ၉၂ ၃၀၅၀ ကျပ် ဖြစ်သည်။",
                pub_date="2026-09-17",
            )

        self.assertEqual([row["value"] for row in inserted_rows], ["3050"])

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


class GlyphEncodedTextLayerTests(unittest.TestCase):
    """A PDF whose fonts carry no Unicode mapping must not count as "text".

    moi.gov.mm serves မြန်မာ့အလင်း issues whose embedded fonts have no
    /ToUnicode CMap, so what every engine calls a text layer is really a run of
    glyph indices. The three literals below are verbatim prefixes from the live
    13 Sep 2026 issue, page 1 (captured via scripts/probe_engines.py):

        pypdf       19,980 chars of "/g194/g196/g201/g201/g3/g3..."
        PyMuPDF      4,319 chars of "\\x02\\x03\\x04\\x04\\x05..."
        pdfplumber  24,673 chars of "(cid:5)3(cid:30)(cid:21)..."

    None of them is blank, so the old `any(t[1].strip() ...)` gate accepted all
    three: the garbage went to Gemini as if it were Burmese, and because the
    page counted as a *text* page the vision pass never saw it. 384 rows — 9% of
    the stored corpus and 58% of every မြန်မာ့အလင်း row — were stored this way,
    while still inflating /status counts.
    """

    # verbatim from the live PDF, trimmed to whole tokens (prefixes; the full
    # pages are homogeneous — artefact ratios 0.550-0.992 over all 32 pages)
    PYPDF = (
        "/g194/g196/g201/g201/g3/g3/g135/g179/g247/g191/g139/g187/g203/g3/g3/g181"
        "/g150/g176/g187/g164/g162/g138/g187/g185/g162/g140/g154/g187/g185/g3/g3"
        "/g195/g3/g161/g134/g187/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3/g3"
        "/g3/g3/g3/g3/g3/g3/g3/g195"
    )
    # \x02..\x1f are raw glyph ids; \t \n \r are the only whitespace the engine
    # emitted. Punctuation such as ! " # $ % & ' ( survives — the rule is
    # ratio-based, so a few non-letter characters cannot rescue the page.
    FITZ = (
        "\u0002\u0003\u0004\u0004\u0005\u0005\u0006\u0007\x08\t\n\u000b\f\u0005"
        "\u0005\r\u000e\u000f\u000b\u0010\u0011\u0012\u000b\u0013\u0011\u0014"
        "\u0015\u000b\u0013\u0005\u0005\u0016\u0005\u0017\u0018\u000b\u0005\u0005"
        "\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005"
        "\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0016\u0019"
        "\u0016\u001a\u0005\u0005\u0006\u0007\x08\t\n\u000b\f\u0005\u0005\n\u0018"
        "\u000b\u000e\u0012\u000b\u001b\u000f\u0005\u0002\u0003\u0005\u0017\u0018"
        "\u000b\f\u0005\u000e\u0015\u001c\u0005\u0005\u001d\r\x08\u001e\r\u0015"
        "\u001f\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005"
        "\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005\u0005"
        "\u0005 \u000e\u001e!\"\u001a#$\f\u0005\u0005 %\t\u000e\u000b\"\u0005"
        "\u0003&\u0003$\n\u0002\u0003\u0004\u0004\u0005\n\u0006\u0007\u0003\x08"
        "\t\n\u000b\f\n\u0002\u0003\u0004\u0004\u0004\r\u000e\n\u000f\u000b\u0010"
        "\t\u0011\u0012\x08\t\n\u0013\u0011\x08\t\n\u0006\u0014\u000f\u000b\u0010"
        "\t\u0006\u0011\u0003\t\u0004\u0004\u0002\u0015\t\u0011\x08\t\u0016\u0003"
        "\u0004\u0004\r\u0017\n'\r\u0015\u001f\u0015(\u0015\u0018\u000b\u0005"
        "\u001a\u0005\u000f\u0017)\u0006\u001e! \u0006*+\u0015\u000b\u0005\u000e"
    )
    # Note the stray letters below ("U", "e"): pdfplumber leaks a few real
    # characters through, which is why the check is a ratio and not "no letters".
    PDFPLUMBER = (
        "(cid:5)3(cid:30)(cid:21)(cid:15)(cid:13)(cid:30)(cid:2)(cid:15)(cid:12)"
        "(cid:13)(cid:22)(cid:21)(cid:14)(cid:15)(cid:22)(cid:9)4(cid:2)(cid:15)"
        "(cid:22)(cid:17)(cid:15)(cid:14)(cid:16))(cid:12)(cid:13)(cid:22)\n"
        "(cid:28)U,(cid:15)\"(cid:2)"
    )

    def test_pypdf_glyph_names_are_noise(self):
        self.assertTrue(ie.looks_like_glyph_noise(self.PYPDF))
        self.assertFalse(ie.is_usable_page_text(self.PYPDF))

    def test_pymupdf_raw_glyph_ids_are_noise(self):
        self.assertTrue(ie.looks_like_glyph_noise(self.FITZ))
        self.assertFalse(ie.is_usable_page_text(self.FITZ))

    def test_pdfplumber_cid_codes_are_noise(self):
        self.assertTrue(ie.looks_like_glyph_noise(self.PDFPLUMBER))
        self.assertFalse(ie.is_usable_page_text(self.PDFPLUMBER))

    def test_real_burmese_is_not_noise(self):
        """The same file's sibling, ကြေးမုံ, decodes perfectly — keep it."""
        page = (
            "၁၃၈၈       ခုနှစ်၊          ေတာ်သလင်းလဆန်း      ၂      ရက်၊ "
            "နိုင်ငံတော်သမ္မတ ဦးမင်းအောင်လှိုင် ပြည်ထောင်စုအစိုးရအဖွဲ့အစည်းအဝေးသို့ "
            "တက်ရောက်အမှာစကားပြောကြား"
        )
        self.assertFalse(ie.looks_like_glyph_noise(page))
        self.assertTrue(ie.is_usable_page_text(page))

    def test_english_and_ascii_pages_are_not_noise(self):
        page = "Sunday, 13 September 2026   Central Bank reference rate 3653-3669"
        self.assertFalse(ie.looks_like_glyph_noise(page))
        self.assertTrue(ie.is_usable_page_text(page))

    def test_clean_issue_with_stray_cid_codes_is_not_noise(self):
        """False-positive guard, with real ကြေးမုံ text.

        pdfplumber emits ``(cid:N)`` for special symbols even in a perfectly
        good issue — this is verbatim page-1 output from the clean 13 Sep 2026
        ကြေးမုံ file, and it is ~5% artefacts. A naive "contains (cid:" rule
        would throw away every page of the good newspaper.
        """
        page = (
            "သွားေလရာအရပ်၌\nအပူေဇာ်ခံရ\nသဒ ါ\ue013တရားလည်းရ၊ှိ သလီ \ue107ငှ လ့် ည်း "
            "ြပညစ့် ၊ုံ\ne - paper ဖတ်\ue108 \ue1f2ရန် အ(cid:490)ခအံ ရလံ ညး"
        )
        self.assertFalse(ie.looks_like_glyph_noise(page))
        self.assertTrue(ie.is_usable_page_text(page))

    def test_the_thresholds_sit_inside_the_measured_gap(self):
        """Lock in the margins measured across 32 pages x 3 engines.

        broken မြန်မာ့အလင်း: artefact 0.550-0.992, letters 0.000-0.080
        clean  ကြေးမုံ:     artefact 0.000-0.093, letters 0.326-0.408
        """
        self.assertGreater(ie._ARTEFACT_RATIO, 0.093)
        self.assertLess(ie._ARTEFACT_RATIO, 0.550)
        self.assertLess(ie._LETTER_RATIO, 0.326)

    def test_numeric_only_page_is_still_usable(self):
        """Guard against over-rejection: a figures-only box is not glyph noise.

        The fuel/gold boxes on page 2 are largely digits. They carry no letter,
        so a naive "no letters => noise" rule would throw away a page that
        ``extract_page2_tables`` may still want.
        """
        page = "92 Ron 3020  3175\n95 Ron 3350  3480\nDiesel 2900  3010\n"
        self.assertFalse(ie.looks_like_glyph_noise(page))
        self.assertTrue(ie.is_usable_page_text(page))

    def test_blank_page_is_not_usable_but_is_not_noise(self):
        for blank in ("", "   ", "\n\t\n"):
            self.assertFalse(ie.looks_like_glyph_noise(blank))
            self.assertFalse(ie.is_usable_page_text(blank))

    def test_a_single_stray_glyph_token_does_not_condemn_a_real_page(self):
        page = "ဈေးနှုန်း ထုတ်ပြန်ချက် /g123 အတွက် ရည်ညွှန်းဈေး ၃၀၅၀ ကျပ်"
        self.assertFalse(ie.looks_like_glyph_noise(page))

    # -- the real mechanism, on a real glyph-encoded PDF ----------------------
    def _make_glyph_pdf(self):
        """A 707-byte PDF whose only font maps codes to glyph names, no ToUnicode.

        This reproduces the moi.gov.mm defect exactly: pypdf resolves the
        /Differences glyph names and, finding nothing in the Adobe Glyph List,
        emits the raw names — the same "/g194/g196/g201" the live မြန်မာ့အလင်း
        issues produce.
        """
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        ]
        stream = b"BT /F1 12 Tf 10 50 Td (ABCD) Tj ET"
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /TestGlyphFont "
            b"/FirstChar 65 /LastChar 68 /Widths [500 500 500 500] "
            b"/Encoding << /Type /Encoding "
            b"/Differences [65 /g194 /g196 /g201 /g3] >> >>"
        )

        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
        for offset in offsets:
            out += ("%010d 00000 n \n" % offset).encode()
        out += (
            b"trailer\n<< /Size " + str(len(objects) + 1).encode()
            + b" /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
        )

        path = os.path.join(self.tmp.name, "glyph_encoded.pdf")
        with open(path, "wb") as handle:
            handle.write(bytes(out))
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_real_glyph_encoded_pdf_reproduces_the_symptom(self):
        from pypdf import PdfReader

        text = PdfReader(self._make_glyph_pdf()).pages[0].extract_text() or ""
        self.assertIn("/g194", text, "fixture must reproduce the live defect")
        self.assertNotIn("A", text, "the glyph names are not real text")
        self.assertFalse(ie.is_usable_page_text(text))

    def test_extract_text_falls_through_to_a_working_engine(self):
        """pypdf is tried first; if its layer is glyph codes we must keep going.

        The old gate stopped at the first engine that returned anything
        non-blank, so a file that pypdf mangles never reached PyMuPDF — even
        when PyMuPDF could read it correctly.
        """
        good = [(1, "ကြေးမုံ သတင်း စာသား"), (2, "")]
        with patch.object(
            ie, "is_usable_page_text", side_effect=ie.is_usable_page_text
        ), patch("pypdf.PdfReader") as reader, patch("fitz.open") as fitz_open:
            reader.return_value.pages = [
                SimpleNamespace(extract_text=lambda: self.PYPDF)
            ]
            # bind t per-iteration; a bare `lambda: t` in a comprehension
            # captures the loop variable and every page would read the last one
            fitz_open.return_value = [
                SimpleNamespace(get_text=lambda text=text: text) for _, text in good
            ]

            result = ie.extract_text_from_pdf("x.pdf")

        self.assertEqual(result, good, "should have fallen through to PyMuPDF")


class GlyphPagesAreRoutedToVisionTests(unittest.TestCase):
    """End-to-end: glyph-encoded pages must reach the vision pass, not the DB.

    ``process_and_ingest_pdf`` decided what was a "text page" with a bare
    ``t.strip()``, so a glyph-encoded issue looked fully digital: the text pass
    ran, the vision pass was skipped, and "/g167/g136/g3..." was stored as news.
    """

    def _run(self, pages, text_articles=None):
        inserted = []
        calls = {"text": [], "vision": []}

        def record_text(page_no, page_text, newspaper, issue_date):
            calls["text"].append(page_no)
            return text_articles or []

        def record_vision(path, newspaper, issue_date):
            calls["vision"].append(path)
            return [{"headline": "OCR ခေါင်းစဉ်", "body_text": "OCR body", "page_no": 1}]

        with patch.object(ie, "extract_page2_tables", return_value=[]), patch.object(
            ie, "extract_text_from_pdf", return_value=pages
        ), patch.object(
            ie, "parse_articles_with_gemini_text", side_effect=record_text
        ), patch.object(
            ie, "parse_articles_with_gemini_native_pdf", side_effect=record_vision
        ), patch.object(
            ie,
            "insert_article_to_supabase",
            side_effect=lambda rec: inserted.append(rec["headline"]) or True,
        ), patch.object(
            ie.os.path, "exists", return_value=True
        ), patch.object(
            ie, "supabase", object()
        ):
            ok = ie.process_and_ingest_pdf("x.pdf", "မြန်မာ့အလင်း", "2026-09-13")
        return ok, calls, inserted

    def test_glyph_only_issue_goes_to_vision_and_skips_the_text_pass(self):
        ok, calls, _ = self._run([(1, GlyphEncodedTextLayerTests.PYPDF)])

        self.assertTrue(ok)
        self.assertEqual(calls["text"], [], "glyph pages must not reach the text pass")
        self.assertTrue(calls["vision"], "glyph pages must be read by vision")

    def test_no_glyph_text_is_ever_inserted(self):
        _, _, inserted = self._run([(1, GlyphEncodedTextLayerTests.PYPDF)])

        for headline in inserted:
            self.assertNotIn("/g", headline)

    def test_mixed_issue_sends_only_the_glyph_pages_to_vision(self):
        """The digital pages keep the text pass; only the broken ones get OCR'd."""
        pages = [(1, "ကြေးမုံ အစစ်အမှန် စာသား"), (2, GlyphEncodedTextLayerTests.FITZ)]
        _, calls, _ = self._run(pages)

        self.assertEqual(calls["text"], [1])
        self.assertTrue(calls["vision"], "the glyph page still needs vision")


class GlyphPageTextGuardTests(unittest.TestCase):
    """Last line of defence inside the text parser itself."""

    def test_the_model_is_not_called_for_a_glyph_encoded_page(self):
        client = MagicMock()
        with patch.object(ie, "gemini_client", client):
            result = ie.parse_articles_with_gemini_text(
                1, GlyphEncodedTextLayerTests.PYPDF, "မြန်မာ့အလင်း", "2026-09-13"
            )

        self.assertEqual(result, [])
        client.models.generate_content.assert_not_called()

    def test_real_text_still_reaches_the_model(self):
        """The guard must not block ordinary pages.

        `gemini_client` is None here, so the parser returns before the model
        call; what this asserts is that a real page is not classified as noise,
        which is the only thing the new guard could wrongly reject.
        """
        with patch.object(ie, "gemini_client", None):
            self.assertTrue(ie.is_usable_page_text("ဈေးနှုန်း ထုတ်ပြန်ချက် ၃၀၅၀ ကျပ်"))
            self.assertFalse(
                ie.is_usable_page_text(GlyphEncodedTextLayerTests.PDFPLUMBER)
            )
            self.assertFalse(ie.is_usable_page_text(GlyphEncodedTextLayerTests.FITZ))


class Page2PriceTableTests(unittest.TestCase):
    """`extract_page2_tables` had ZERO coverage — which is how a dead branch lived.

    Two defects were found by exercising it for the first time:

    1. The fuel and gold checks were `if` / `elif`. The real page-2 box holds
       both (its own headline reads "…ဓာတ်သတ္တု(ရွှေ)၊ စက်သုံးဆီ နှင့်
       နိုင်ငံခြားငွေလဲ…"), so the fuel branch always won and the gold row was
       never emitted. Live evidence: `newspaper_numbers` contains **0** rows with
       `section='ရွှေ'`, though the branch has existed all along.
    2. It returned `[]` completely silently, so a paper whose page-2 fonts have no
       Unicode mapping looked exactly like a paper with no price box.
    """

    FUEL_ONLY = "ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ\nOctane 92 | 3050"
    GOLD_ONLY = "ဓာတ်သတ္တု(ရွှေ)ရည်ညွှန်းဈေးသတ်မှတ်ရေးကော်မတီ ရည်ညွှန်းဈေး\n၇၁၅၀၀၀၀"
    # One box carrying both, exactly like the real paper.
    COMBINED = (
        "ရက်နေ့ရှိ ဓာတ်သတ္တု(ရွှေ)၊ စက်သုံးဆီ နှင့် နိုင်ငံခြားငွေလဲလှယ်နှုန်းများ\n"
        "ရွှေတစ်ကျပ်သား ရည်ညွှန်းဈေး ၇၁၅၀၀၀၀ ကျပ်\nOctane 92 ရန်ကုန် ၃၀၅၀ ကျပ်"
    )

    def _run(self, tables, page_count=2):
        page2 = SimpleNamespace(extract_tables=lambda: tables)
        pages = [SimpleNamespace(extract_tables=lambda: []) for _ in range(page_count)]
        if page_count >= 2:
            pages[1] = page2
        fake_pdf = MagicMock()
        fake_pdf.pages = pages
        opener = MagicMock()
        opener.__enter__ = MagicMock(return_value=fake_pdf)
        opener.__exit__ = MagicMock(return_value=False)

        with patch.object(ie, "pdfplumber") as plumber, patch.object(
            ie.os.path, "exists", return_value=True
        ):
            plumber.open.return_value = opener
            return ie.extract_page2_tables("x.pdf")

    def test_a_combined_box_emits_both_the_fuel_and_gold_rows(self):
        """The regression test for the `if`/`elif` bug."""
        rows = self._run([[["x"], [self.COMBINED]]])

        self.assertEqual([r["section"] for r in rows], ["စက်သုံးဆီ", "ရွှေ"])

    def test_a_fuel_only_box_emits_one_row(self):
        rows = self._run([[["x"], [self.FUEL_ONLY]]])

        self.assertEqual([r["section"] for r in rows], ["စက်သုံးဆီ"])

    def test_a_gold_only_box_emits_one_row(self):
        rows = self._run([[["x"], [self.GOLD_ONLY]]])

        self.assertEqual([r["section"] for r in rows], ["ရွှေ"])

    def test_an_unmatched_page_is_reported_instead_of_failing_silently(self):
        with self.assertLogs("IngestionPipeline", level="WARNING") as caught:
            rows = self._run([[["x"], ["ကြော်ငြာ"]]])

        self.assertEqual(rows, [])
        self.assertTrue(
            any("yielded no fuel/gold table" in line for line in caught.output),
            "a missing price table must be visible in the logs",
        )

    def test_single_page_pdf_returns_empty(self):
        self.assertEqual(self._run([[["x"]]], page_count=1), [])

    def test_missing_pdfplumber_returns_empty(self):
        with patch.object(ie, "pdfplumber", None):
            self.assertEqual(ie.extract_page2_tables("x.pdf"), [])

    def test_the_real_mangled_keywords_do_not_match(self):
        """Pins a KNOWN limitation, using text captured from the live PDF.

        ကြေးမုံ's page-2 gold box *is* detected as a table, but its text layer
        mangles precisely the characters the check looks for: "ရွှေ" comes out as
        "ေရ(cid:619)" and "ရည်ညွှန်း" as "ရည်\\ue101(cid:623) န်း". No string match
        can succeed, which is why this function returns [] for BOTH newspapers
        today. If a future fix makes these match, this test should be updated
        rather than deleted.
        """
        mangled = (
            "\n၁၂ - ၉ -၂၀၂၆ ရက်ေန(cid:485) ဓာတ်သတ\ue01f \ue2f1(ေရ(cid:619) )"
            "ရည်\ue101(cid:623) န်းေ ဈး\nသိပ်သည်းဆ ၁၉.၂၅ ဂရမ်/ ကုဗစင်တီမီတာ"
        )
        self.assertNotIn("ရွှေ", mangled)
        self.assertNotIn("ရည်ညွှန်း", mangled)
        self.assertNotIn("စက်သုံးဆီ", mangled)
        self.assertEqual(self._run([[["x"], [mangled]]]), [])


if __name__ == "__main__":
    unittest.main()
