"""
================================================================
MY_REPORTER AUTOMATED NEWSPAPER INGESTION PIPELINE (v2.1)
- Model Fallback Loop: ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-flash-latest"]
- Ingests Articles to Supabase `articles` view
- Extracts Page 2 Table Boxes (Fuel & Gold) via pdfplumber
- Auto-extracts precision figures into `newspaper_numbers`
================================================================
"""

import os
import re
import json
import logging
from datetime import datetime

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

import pdfplumber

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("IngestionPipeline")

# Configuration
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zdsxuxwonkovuesjepfa.supabase.co")
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")
)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# User-Specified Gemini Models in Fallback Priority Order
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-flash-latest"
]

# Initialize Supabase Client
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
    if not gemini_client or not supabase or len(body_text.strip()) < 15:
        return

    # Check if text contains digits
    burmese_digits = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    norm_text = body_text.translate(burmese_digits)
    if not re.search(r"\d{2,}", norm_text):
        return

    prompt = f"""
Extract all specific prices, rates, yields, or metrics from this text:
Date: {pub_date}
Headline: {headline}
Text: {body_text[:4000]}

Return ONLY a valid JSON array:
[
  {{
    "context": "အတိအကျ အညွှန်း (ဥပမာ - Octane 92 ရည်ညွှန်းလက်ကားဈေး)",
    "value": "အင်္ဂလိပ်ဂဏန်းသီးသန့် (ဥပမာ - 3050)",
    "original_value": "မူရင်း မြန်မာဂဏန်း (ဥပမာ - ၃,၀၅၀)",
    "unit": "ကျပ် သို့မဟုတ် ယူနစ်",
    "source_text": "မူရင်း စာကြောင်းတို"
  }}
]
If none, return [].
"""
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0) if types else None

    for model_name in GEMINI_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            if res and res.text:
                cleaned = re.sub(r"^```json\s*|\s*```$", "", res.text.strip(), flags=re.MULTILINE)
                items = json.loads(cleaned)
                if isinstance(items, list) and items:
                    rows = []
                    for it in items:
                        v = str(it.get("value", "")).replace(",", "").strip()
                        v_match = re.search(r"[-+]?\d+(?:\.\d+)?", v)
                        rows.append({
                            "publication_date": pub_date,
                            "headline": headline,
                            "section": section,
                            "context": it.get("context", ""),
                            "value": v_match.group(0) if v_match else v,
                            "original_value": it.get("original_value", ""),
                            "unit": it.get("unit", ""),
                            "source_text": it.get("source_text", "")
                        })
                    supabase.from_("newspaper_numbers").insert(rows).execute()
                    logger.info("💰 Saved %d numeric facts into newspaper_numbers [%s].", len(rows), model_name)
                    return
        except Exception as e:
            logger.warning("Numeric auto-extract error [%s]: %s", model_name, e)
            continue


def extract_page2_tables(pdf_path: str) -> list[dict]:
    """Page 2 ၏ Boxed Tables (စက်သုံးဆီ နှင့် ရွှေဈေး) များကို pdfplumber ဖြင့် သီးသန့်ဆွဲထုတ်ခြင်း"""
    results = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if len(pdf.pages) < 2:
                return []
            page = pdf.pages[1]  # Page 2 (0-indexed)
            tables = page.extract_tables()
            for t in tables:
                rows = [" | ".join([str(c).strip() for c in r if c]) for r in t if r]
                full = "\n".join(rows)
                if any(k in full for k in ["စက်သုံးဆီ", "ရည်ညွှန်းလက်ကား", "Octane", "Diesel", "ဒီဇယ်"]):
                    results.append({
                        "headline": "ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ",
                        "body_text": f"ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ။\n{full}",
                        "page_no": 2
                    })
                elif "ရွှေ" in full and "ရည်ညွှန်း" in full:
                    results.append({
                        "headline": "ဓာတ်သတ္တု(ရွှေ)ရည်ညွှန်းဈေးသတ်မှတ်ရေးကော်မတီ ရည်ညွှန်းဈေး",
                        "body_text": full,
                        "page_no": 2
                    })
    except Exception as e:
        logger.warning("Page 2 table extraction warning: %s", e)
    return results


def parse_articles_with_gemini_text(page_no: int, page_text: str, newspaper_name: str, issue_date: str) -> list[dict]:
    """သတ်မှတ်ထားသော GEMINI_MODELS အစဉ်အတိုင်း စာမျက်နှာအလိုက် သတင်းများကို ခွဲထုတ်ခြင်း"""
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

    # Model Loop Fallback
    for model_name in GEMINI_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=config
            )
            if res and res.text:
                cleaned = re.sub(r"^```json\s*|\s*```$", "", res.text.strip(), flags=re.MULTILINE)
                articles = json.loads(cleaned)
                if isinstance(articles, list) and articles:
                    return articles
        except Exception as e:
            logger.warning("Page %d parse error [%s]: %s", page_no, model_name, e)
            continue

    return []


def process_and_ingest_pdf(pdf_path: str, newspaper_name: str, issue_date: str) -> bool:
    """သတင်းစာ PDF တစ်စောင်လုံးကို Ingest ပြုလုပ်သည့် အဓိက လုပ်ဆောင်ချက်"""
    logger.info("🚀 Processing Ingestion: %s (%s) | %s", newspaper_name, issue_date, pdf_path)

    if not os.path.exists(pdf_path) or not supabase:
        logger.error("❌ PDF file or Supabase connection missing.")
        return False

    total_ingested = 0

    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            logger.info("📄 Total pages in PDF: %d", total_pages)

            for idx, page in enumerate(pdf.pages):
                page_no = idx + 1
                page_text = page.extract_text() or ""

                # ----------------------------------------------------
                # SPECIAL HANDLER FOR PAGE 2 (Fuel & Gold Tables)
                # ----------------------------------------------------
                if page_no == 2:
                    p2_tables = extract_page2_tables(pdf_path)
                    for tbl in p2_tables:
                        rec = {
                            "newspaper_name": newspaper_name,
                            "issue_date": issue_date,
                            "page_no": 2,
                            "headline": tbl["headline"],
                            "body_text": tbl["body_text"]
                        }
                        if insert_article_to_supabase(rec):
                            total_ingested += 1
                            extract_numbers_into_db(tbl["headline"], tbl["body_text"], issue_date, "စက်သုံးဆီ")

                # ----------------------------------------------------
                # REGULAR ARTICLES ON PAGE
                # ----------------------------------------------------
                if page_text.strip():
                    articles = parse_articles_with_gemini_text(page_no, page_text, newspaper_name, issue_date)
                    for art in articles:
                        h = str(art.get("headline", "")).strip()
                        b = str(art.get("body_text", "")).strip()
                        if h and b and len(b) > 20:
                            rec = {
                                "newspaper_name": newspaper_name,
                                "issue_date": issue_date,
                                "page_no": int(art.get("page_no") or page_no),
                                "headline": h,
                                "body_text": b
                            }
                            if insert_article_to_supabase(rec):
                                total_ingested += 1
                                # Auto-extract numbers if article is economic or statistical
                                if any(k in h for k in ["ဈေး", "နှုန်း", "ရင်းနှီးမြှုပ်နှံမှု", "စပါး", "ဘဏ္ဍာ"]):
                                    extract_numbers_into_db(h, b, issue_date, "စီးပွားရေး")

        logger.info("✅ Ingestion Complete: %d articles inserted for %s (%s).", total_ingested, newspaper_name, issue_date)
        return total_ingested > 0

    except Exception as e:
        logger.error("❌ Ingestion failed: %s", e, exc_info=True)
        return False


# ============================================================
# RUN INGESTION DIRECTLY (CLI)
# ============================================================
if __name__ == "__main__":
    import sys
    # Example usage: python ingest_newspaper.py "path/to/file.pdf" "မြန်မာ့အလင်း" "2026-09-14"
    if len(sys.argv) >= 4:
        file_p = sys.argv[1]
        paper_n = sys.argv[2]
        date_s = sys.argv[3]
        process_and_ingest_pdf(file_p, paper_n, date_s)
    else:
        print("ℹ️ အသုံးပြုနည်း: python ingest_newspaper.py <PDF_PATH> <NEWSPAPER_NAME> <YYYY-MM-DD>")