import os
import re
import json
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

# Configuration
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zdsxuxwonkovuesjepfa.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Initialize Supabase
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY and create_client:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"⚠️ Supabase Init Error: {e}")

# Initialize Gemini Client
gemini_client = None
if GEMINI_API_KEY and genai:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"⚠️ Gemini Init Error: {e}")

GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-flash-latest"]


def insert_article_to_supabase(record):
    """
    'articles' View သို့ တိုက်ရိုက် Clean Insert ပြုလုပ်ခြင်း
    """
    if not supabase:
        return False
    try:
        res = supabase.table("articles").insert(record).execute()
        return bool(res.data)
    except Exception as err:
        print(f"⚠️ Insert Error: {err}")
        return False


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
            temperature=0.1
        )

        for model_name in GEMINI_MODELS:
            try:
                res = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[pdf_part, prompt],
                    config=config
                )
                if res and res.text:
                    cleaned = re.sub(r'^```json\s*|\s*```$', '', res.text.strip(), flags=re.MULTILINE)
                    articles = json.loads(cleaned)
                    if isinstance(articles, list):
                        return articles
            except Exception as e:
                print(f"⚠️ Gemini Native PDF Vision Warning [{model_name}]: {e}")
                continue

    except Exception as err:
        print(f"⚠️ Native PDF Processing Exception: {err}")

    return []


def parse_articles_with_gemini_text(page_no, page_text, newspaper_name, issue_date):
    """Text-based PDF များအတွက် Gemini AI Parsing"""
    if not gemini_client or not page_text.strip():
        return []

    prompt = f"""
Extract all Myanmar news articles from "{newspaper_name}" published on {issue_date} (Page {page_no}).

Text:
\"\"\"
{page_text[:10000]}
\"\"\"

Return strictly JSON array:
[
  {{"headline": "ခေါင်းစဉ်", "body_text": "သတင်းစာကိုယ်...", "page_no": {page_no}}}
]
"""
    config = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1) if types else None

    for model_name in GEMINI_MODELS:
        try:
            res = gemini_client.models.generate_content(
                model=model_name, contents=prompt, config=config
            )
            if res and res.text:
                cleaned = re.sub(r'^```json\s*|\s*```$', '', res.text.strip(), flags=re.MULTILINE)
                articles = json.loads(cleaned)
                if isinstance(articles, list):
                    return articles
        except Exception:
            continue
    return []


def process_and_ingest_pdf(pdf_path, newspaper_name, issue_date):
    """PDF ဖိုင်မှ မြန်မာ့အလင်း နှင့် ကြေးမုံ သတင်းများကို Supabase သို့ တိုက်ရိုက် ထည့်သွင်းခြင်း"""
    print(f"🚀 Processing Ingestion: {newspaper_name} ({issue_date}) | File: {pdf_path}")
    
    if not os.path.exists(pdf_path) or not supabase:
        print("❌ File or Supabase client missing.")
        return False

    pages = extract_text_from_pdf(pdf_path)
    total_ingested = 0

    # 1. Standard Text Extraction ရပါက Text Mode သုံးမည်
    if pages and any(p[1].strip() for p in pages):
        for page_no, page_text in pages:
            if not page_text.strip(): continue
            articles = parse_articles_with_gemini_text(page_no, page_text, newspaper_name, issue_date)
            for art in articles:
                if art.get("headline") and art.get("body_text"):
                    record = {
                        "newspaper_name": str(newspaper_name),
                        "issue_date": str(issue_date),
                        "page_no": int(art.get("page_no") or page_no),
                        "headline": str(art.get("headline", "")).strip(),
                        "body_text": str(art.get("body_text", "")).strip()
                    }
                    if insert_article_to_supabase(record):
                        total_ingested += 1

    # 2. Text extraction မရပါက (Scanned Image PDF ဖြစ်ပါက) Gemini Native PDF Vision သုံးမည်
    else:
        print(f"📸 Standard Text extraction yielded no text. Switching to Gemini Native PDF Vision for {newspaper_name}...")
        articles = parse_articles_with_gemini_native_pdf(pdf_path, newspaper_name, issue_date)
        for art in articles:
            if art.get("headline") and art.get("body_text"):
                record = {
                    "newspaper_name": str(newspaper_name),
                    "issue_date": str(issue_date),
                    "page_no": int(art.get("page_no") or 1),
                    "headline": str(art.get("headline", "")).strip(),
                    "body_text": str(art.get("body_text", "")).strip()
                }
                if insert_article_to_supabase(record):
                    total_ingested += 1

    print(f"✅ Successful Ingestion: {total_ingested} articles inserted for {newspaper_name} ({issue_date}).")
    return total_ingested > 0