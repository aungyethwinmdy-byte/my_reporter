"""
================================================================
MY_REPORTER AUTOMATED NEWSPAPER INGESTION PIPELINE
- Model Fallback Loop via gemini_config
- Ingests Articles to Supabase `articles` view
- Extracts Page 2 Table Boxes (Fuel & Gold) via pdfplumber
- Auto-extracts precision figures into `newspaper_numbers`
================================================================
"""

import os
import re
import json
import logging

from env_config import get_gemini_api_key, get_supabase_credentials

# Environment Variables Loading
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Official Google GenAI SDK (google-genai)
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# Supabase Client
try:
    from supabase import create_client, Client
except ImportError:
    create_client = None
    Client = None

# PDF Plumber (optional/guarded)
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from auto_numeric_extractor import extract_numbers_from_article, clean_number

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("IngestionPipeline")

# Configuration
# NOTE: env only — hardcoded fallbacks silently point ingestion at the wrong
# Supabase project. Resolution lives in env_config so all five callers agree.
SUPABASE_URL, SUPABASE_KEY = get_supabase_credentials()
GEMINI_API_KEY = get_gemini_api_key()

# Initialize Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY and create_client:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        logger.error("⚠️ Supabase Init Error: %s", e)

# Initialize Gemini Client
gemini_client = None
if GEMINI_API_KEY and genai:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error("⚠️ Gemini Init Error: %s", e)

# Model selection lives in gemini_config so every module (this ingest engine,
# the Telegram bot, the verifier, the numeric extractor) reads the same
# GEMINI_MODEL / GEMINI_FALLBACK_MODELS values and uses the same fallback chain.
from gemini_config import (
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GEMINI_MODELS,
    generate_content_with_fallback,
)

__all__ = [
    "GEMINI_FALLBACK_MODELS",
    "GEMINI_MODEL",
    "GEMINI_MODELS",
    "process_and_ingest_pdf",
    "extract_text_from_pdf",
    "parse_articles_with_gemini_native_pdf",
    "parse_articles_with_gemini_text",
    "extract_numbers_into_db",
    "extract_page2_tables",
    "insert_article_to_supabase",
]


def _parse_article_list(text):
    """Strip an optional ```json fence and require a JSON array."""
    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    articles = json.loads(cleaned)
    if not isinstance(articles, list):
        raise ValueError("Gemini did not return a JSON array")
    return articles


def insert_article_to_supabase(record: dict) -> bool:
    """'articles' View သို့ INSTEAD OF Trigger မှတစ်ဆင့် Insert ပြုလုပ်ခြင်း"""
    if not supabase:
        return False
    try:
        res = supabase.table("articles").insert(record).execute()
        return bool(res.data)
    except Exception as err:
        logger.warning("⚠️ Article Insert Warning: %s", err)
        return False


def extract_numbers_into_db(headline: str, body_text: str, pub_date: str, section: str = "စီးပွားရေး"):
    """သတင်းထဲမှ ကိန်းဂဏန်းနှင့် ဈေးနှုန်းများကို newspaper_numbers သို့ အလိုအလျောက် သွင်းယူခြင်း"""
    if not gemini_client or not supabase or not body_text or len(body_text.strip()) < 15:
        return

    # Check if text contains digits (Burmese or English)
    burmese_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm_text = body_text.translate(burmese_digits)
    if not re.search(r"\d{2,}", norm_text):
        return

    try:
        items = extract_numbers_from_article(
            headline=headline,
            article_text=body_text,
            publication_date=pub_date,
            section=section,
            genai_client=gemini_client,
        )
        if items and isinstance(items, list):
            rows = []
            for it in items:
                v = clean_number(str(it.get("value", "")))
                rows.append({
                    "publication_date": pub_date,
                    "headline": headline,
                    "section": section,
                    "context": it.get("context", ""),
                    "value": v,
                    "original_value": it.get("original_value", ""),
                    "unit": it.get("unit", ""),
                    "source_text": it.get("source_text", ""),
                })
            if rows:
                supabase.from_("newspaper_numbers").insert(rows).execute()
                logger.info("💰 Saved %d numeric facts into newspaper_numbers.", len(rows))
    except Exception as e:
        logger.warning("Numeric auto-extract error: %s", e)


def extract_page2_tables(pdf_path: str) -> list[dict]:
    """Page 2 ၏ Boxed Tables (စက်သုံးဆီ နှင့် ရွှေဈေး) များကို pdfplumber ဖြင့် သီးသန့်ဆွဲထုတ်ခြင်း"""
    results = []
    if not pdfplumber or not os.path.exists(pdf_path):
        return []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if len(pdf.pages) < 2:
                return []
            page = pdf.pages[1]  # Page 2 (0-indexed)
            tables = page.extract_tables() or []
            for t in tables:
                rows = [" | ".join([str(c).strip() for c in r if c]) for r in t if r]
                full = "\n".join(rows)
                if any(k in full for k in ["စက်သုံးဆီ", "ရည်ညွှန်းလက်ကား", "Octane", "Diesel", "ဒီဇယ်"]):
                    results.append({
                        "headline": "ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ",
                        "body_text": f"ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ။\n{full}",
                        "page_no": 2,
                        "section": "စက်သုံးဆီ",
                    })
                elif "ရွှေ" in full and "ရည်ညွှန်း" in full:
                    results.append({
                        "headline": "ဓာတ်သတ္တု(ရွှေ)ရည်ညွှန်းဈေးသတ်မှတ်ရေးကော်မတီ ရည်ညွှန်းဈေး",
                        "body_text": full,
                        "page_no": 2,
                        "section": "ရွှေ",
                    })
    except Exception as e:
        logger.warning("Page 2 table extraction warning: %s", e)
    return results


def extract_text_from_pdf(pdf_path):
    """PDF ဖိုင်မှ စာသားများကို Standard Engine များဖြင့် ထုတ်ယူခြင်း"""
    pages_text = []

    # 1. Try PyPDF
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        for idx, page in enumerate(reader.pages):
            txt = page.extract_text() or ""
            pages_text.append((idx + 1, txt))
        if any(t[1].strip() for t in pages_text):
            return pages_text
    except Exception:
        pass

    # 2. Try PyMuPDF (fitz)
    try:
        import fitz
        doc = fitz.open(pdf_path)
        pages_text = []
        for idx, page in enumerate(doc):
            pages_text.append((idx + 1, page.get_text()))
        if any(t[1].strip() for t in pages_text):
            return pages_text
    except Exception:
        pass

    # 3. Try pdfplumber
    if pdfplumber:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                pages_text = []
                for idx, page in enumerate(pdf.pages):
                    txt = page.extract_text() or ""
                    pages_text.append((idx + 1, txt))
                if any(t[1].strip() for t in pages_text):
                    return pages_text
        except Exception:
            pass

    logger.warning(
        "⚠️ pypdf/PyMuPDF/pdfplumber ဖြင့် စာသား ထုတ်ယူ၍ မရပါ "
        "(library မရှိခြင်း သို့မဟုတ် scanned PDF ဖြစ်ခြင်း)။ "
        "Gemini Native PDF Vision သို့ ပြောင်းပါမည်。"
    )
    return pages_text


def parse_articles_with_gemini_native_pdf(pdf_path, newspaper_name, issue_date):
    """Scanned/Image PDF ဖိုင်တစ်ခုလုံးကို Gemini သို့ Native PDF အနေဖြင့် တိုက်ရိုက် ပို့ပေးခြင်း"""
    if not gemini_client or not types or not os.path.exists(pdf_path):
        return []

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        prompt = f"""
You are an expert news editor in Myanmar.
Read this complete PDF newspaper file from "{newspaper_name}" published on {issue_date}.

CRITICAL INSTRUCTIONS:
1. Extract all individual news articles across all pages in this newspaper PDF.
2. Output strictly a JSON array of objects.
3. Each object MUST have:
   - "headline": Concise Burmese headline of the article.
   - "body_text": Complete text content of the news article.
   - "page_no": Estimated page number (integer, default 1 if unknown).

Return strictly JSON in this format:
[
  {{"headline": "ခေါင်းစဉ်", "body_text": "သတင်းစာကိုယ် စာသား...", "page_no": 1}}
]
"""
        pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        )

        return generate_content_with_fallback(
            gemini_client,
            contents=[pdf_part, prompt],
            config=config,
            parse=_parse_article_list,
        )

    except Exception as err:
        logger.warning("⚠️ Native PDF Processing Exception: %s", err)

    return []


def parse_articles_with_gemini_text(page_no, page_text, newspaper_name, issue_date):
    """Text-based PDF များအတွက် Gemini AI Parsing"""
    if not gemini_client or not page_text.strip():
        return []

    prompt = f"""
Extract all Myanmar news articles from "{newspaper_name}" published on {issue_date} (Page {page_no}).

Text:
\"\"\"
{page_text[:12000]}
\"\"\"

Return strictly JSON array:
[
  {{"headline": "ခေါင်းစဉ်အပြည့်အစုံ", "body_text": "သတင်းစာကိုယ် အပြည့်အစုံ...", "page_no": {page_no}}}
]
"""
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1) if types else None

    try:
        return generate_content_with_fallback(
            gemini_client, contents=prompt, config=config, parse=_parse_article_list
        )
    except Exception as err:
        logger.warning("⚠️ Gemini text parsing failed for page %d: %s", page_no, err)
        return []


def process_and_ingest_pdf(pdf_path, newspaper_name, issue_date):
    """သတင်းစာ PDF တစ်စောင်လုံးကို Ingest ပြုလုပ်သည့် အဓိက လုပ်ဆောင်ချက်"""
    logger.info("🚀 Processing Ingestion: %s (%s) | File: %s", newspaper_name, issue_date, pdf_path)

    if not os.path.exists(pdf_path) or not supabase:
        logger.error("❌ File or Supabase client missing.")
        return False

    total_ingested = 0

    # 1. Page-2 fuel/gold table boxes via pdfplumber (ingested first)
    p2_tables = extract_page2_tables(pdf_path)
    for tbl in p2_tables:
        sec = tbl.get("section", "စက်သုံးဆီ")
        rec = {
            "newspaper_name": str(newspaper_name),
            "issue_date": str(issue_date),
            "page_no": 2,
            "headline": str(tbl["headline"]),
            "body_text": str(tbl["body_text"]),
        }
        if insert_article_to_supabase(rec):
            total_ingested += 1
            extract_numbers_into_db(tbl["headline"], tbl["body_text"], issue_date, sec)

    # 2. Extract text from PDF
    pages = extract_text_from_pdf(pdf_path)

    # A newspaper PDF is frequently *partially* scanned: some pages carry an
    # embedded text layer while others are pure images. The old branch only
    # asked "did ANY page yield text?" and then handled the whole document in
    # text mode, silently discarding every image-only page — for these papers
    # that is often page 2, where the fuel and gold price tables live. Track the
    # pages that produced nothing and OCR those specifically.
    text_pages = [(n, t) for (n, t) in pages if t and t.strip()]
    blank_pages = [n for (n, t) in pages if not (t and t.strip())]

    # 3. Standard Text Extraction ရပါက Text Mode သုံးမည်
    if text_pages:
        for page_no, page_text in text_pages:
            articles = parse_articles_with_gemini_text(page_no, page_text, newspaper_name, issue_date)
            for art in articles:
                h = str(art.get("headline", "")).strip()
                b = str(art.get("body_text", "")).strip()
                if h and b:
                    rec = {
                        "newspaper_name": str(newspaper_name),
                        "issue_date": str(issue_date),
                        "page_no": int(art.get("page_no") or page_no),
                        "headline": h,
                        "body_text": b,
                    }
                    if insert_article_to_supabase(rec):
                        total_ingested += 1
                        if any(k in h for k in ["ဈေး", "နှုန်း", "ရင်းနှီးမြှုပ်နှံမှု", "စပါး", "ဘဏ္ဍာ"]):
                            extract_numbers_into_db(h, b, issue_date, "စီးပွားရေး")

    # 4. စာသားမရသော စာမျက်နှာများ (Scanned Image Pages) ကို Gemini Native PDF
    #    Vision ဖြင့် သီးသန့် ဖတ်မည်။ pages လုံးဝမရပါကလည်း ဤနေရာသို့ ရောက်သည်။
    if blank_pages or not text_pages:
        logger.info(
            "📸 %d page(s) yielded no text layer; using Gemini Native PDF Vision for %s...",
            len(blank_pages), newspaper_name,
        )
        articles = parse_articles_with_gemini_native_pdf(pdf_path, newspaper_name, issue_date)
        for art in articles:
            h = str(art.get("headline", "")).strip()
            b = str(art.get("body_text", "")).strip()
            if h and b:
                rec = {
                    "newspaper_name": str(newspaper_name),
                    "issue_date": str(issue_date),
                    "page_no": int(art.get("page_no") or 1),
                    "headline": h,
                    "body_text": b,
                }
                if insert_article_to_supabase(rec):
                    total_ingested += 1
                    if any(k in h for k in ["ဈေး", "နှုန်း", "ရင်းနှီးမြှုပ်နှံမှု", "စပါး", "ဘဏ္ဍာ"]):
                        extract_numbers_into_db(h, b, issue_date, "စီးပွားရေး")

    logger.info("✅ Successful Ingestion: %d articles inserted for %s (%s).", total_ingested, newspaper_name, issue_date)
    return total_ingested > 0


if __name__ == "__main__":
    import sys
    # Example usage: python ingest_engine.py "path/to/file.pdf" "မြန်မာ့အလင်း" "2026-09-14"
    if len(sys.argv) >= 4:
        file_p = sys.argv[1]
        paper_n = sys.argv[2]
        date_s = sys.argv[3]
        process_and_ingest_pdf(file_p, paper_n, date_s)
    else:
        print("ℹ️ အသုံးပြုနည်း: python ingest_engine.py <PDF_PATH> <NEWSPAPER_NAME> <YYYY-MM-DD>")
