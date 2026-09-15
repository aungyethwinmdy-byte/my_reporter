"""
================================================================
CROSS-SOURCE FACT-CHECK & DIVERGENCE DETECTOR - Phase 1 (Resilient)
================================================================
File Name: cross_source_verifier.py
================================================================
"""

import os
import re
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("CrossSourceVerifier")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
)

INDEPENDENT_SUPABASE_URL = os.getenv("INDEPENDENT_SUPABASE_URL") or SUPABASE_URL
INDEPENDENT_SUPABASE_KEY = os.getenv("INDEPENDENT_SUPABASE_KEY") or SUPABASE_KEY
INDEPENDENT_TABLE_NAME = os.getenv("INDEPENDENT_TABLE_NAME", "independent_articles")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

STATE_MEDIA_SOURCES = ["မြန်မာ့အလင်း", "ကြေးမုံ"]
SEARCH_WINDOW_DAYS = int(os.getenv("SEARCH_WINDOW_DAYS", 14))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

state_db: Optional[Client] = None
independent_db: Optional[Client] = None
genai_client: Optional[genai.Client] = None

try:
    if SUPABASE_URL and SUPABASE_KEY:
        state_db = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ State Media DB connected")
except Exception as e:
    logger.error(f"❌ State Media DB init failed: {e}")

try:
    if INDEPENDENT_SUPABASE_URL == SUPABASE_URL and state_db:
        independent_db = state_db
        logger.info("ℹ️ Independent DB uses the same Supabase client")
    elif INDEPENDENT_SUPABASE_URL and INDEPENDENT_SUPABASE_KEY:
        independent_db = create_client(INDEPENDENT_SUPABASE_URL, INDEPENDENT_SUPABASE_KEY)
        logger.info("✅ Independent Media DB connected")
except Exception as e:
    logger.error(f"❌ Independent Media DB init failed: {e}")

try:
    if GEMINI_API_KEY:
        genai_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info(f"✅ Gemini client initialized (Model: {GEMINI_MODEL})")
except Exception as e:
    logger.error(f"❌ Gemini init failed: {e}")


def sanitize_keyword(kw: str) -> str:
    cleaned = re.sub(r'[,()\'"%\*\\]', '', kw)
    return cleaned.strip()


def safe_json_extract(text: str) -> List[str]:
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
    return []


def generate_with_retry(prompt: str, config: types.GenerateContentConfig, max_retries: int = 3) -> str:
    delay = 2
    for attempt in range(max_retries):
        try:
            response = genai_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config
            )
            return response.text.strip()
        except Exception as e:
            err_str = str(e)
            if "503" in err_str or "UNAVAILABLE" in err_str:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️ Gemini 503 high demand. Retrying in {delay}s... ({attempt+1}/{max_retries})")
                    time.sleep(delay)
                    delay *= 2
                    continue
            raise e
    raise RuntimeError("Gemini API call failed after retries.")


def plan_comparison_keywords(topic: str) -> List[str]:
    if not genai_client:
        return [sanitize_keyword(w) for w in topic.split() if len(w) > 1][:5]

    prompt = f"""You are a Myanmar news search expert.
Extract 4-6 essential Myanmar search keywords from this topic for database full-text matching.
Topic: "{topic}"

Rules:
1. Every keyword must be in standard Myanmar script.
2. Focus on proper nouns: locations, prominent figures, organizations, key events.
3. Exclude conversational filler words.
4. Return a valid JSON array only.
Example: ["မြဝတီ", "ကုန်သွယ်ရေး", "နယ်စပ်ဂိတ်"]
"""
    try:
        cfg = types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
        result_text = generate_with_retry(prompt, cfg)
        parsed = safe_json_extract(result_text)
        if isinstance(parsed, list) and parsed:
            keywords = [sanitize_keyword(k) for k in parsed if isinstance(k, str) and sanitize_keyword(k)]
            logger.info(f"🔑 Keywords planned: {keywords}")
            return keywords[:6]
    except Exception as e:
        logger.error(f"❌ Query planner failed: {e}")

    fallback = [sanitize_keyword(w) for w in topic.split() if len(w) > 1]
    return [k for k in fallback if k][:5]


def fetch_state_media_articles(keywords: List[str], limit: int = 8) -> List[Dict]:
    if not state_db or not keywords:
        return []

    cutoff_date = (datetime.now() - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    all_articles = []
    seen_ids = set()

    for kw in keywords[:5]:
        if not kw:
            continue
        try:
            result = state_db.from_("articles") \
                .select("article_id, newspaper_name, issue_date, page_no, headline, body_text") \
                .gte("issue_date", cutoff_date) \
                .or_(f"headline.ilike.%{kw}%,body_text.ilike.%{kw}%") \
                .order("issue_date", desc=True) \
                .limit(limit) \
                .execute()

            for art in (result.data or []):
                aid = art.get("article_id")
                paper = art.get("newspaper_name", "")
                if any(sm in paper for sm in STATE_MEDIA_SOURCES):
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        all_articles.append(art)
        except Exception as e:
            logger.warning(f"⚠️ State search failed for '{kw}': {e}")
            continue

    logger.info(f"📰 State media fetched: {len(all_articles)}")
    return all_articles[:limit]


def fetch_independent_media_articles(keywords: List[str], limit: int = 8) -> List[Dict]:
    if not independent_db or not keywords:
        return []

    cutoff_date = (datetime.now() - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    all_articles = []
    seen_ids = set()

    for kw in keywords[:5]:
        if not kw:
            continue
        try:
            result = independent_db.from_(INDEPENDENT_TABLE_NAME) \
                .select("id, source_name, published_date, headline, body_text, url, category") \
                .gte("published_date", cutoff_date) \
                .or_(f"headline.ilike.%{kw}%,body_text.ilike.%{kw}%") \
                .order("published_date", desc=True) \
                .limit(limit) \
                .execute()

            for art in (result.data or []):
                aid = art.get("id") or art.get("url")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    all_articles.append(art)
        except Exception as e:
            err_msg = str(e)
            if "PGRST205" in err_msg or "Could not find the table" in err_msg or "404" in err_msg:
                logger.warning(f"⚠️ Table '{INDEPENDENT_TABLE_NAME}' cache not refreshed or table missing. Check SQL notify.")
                return []
            logger.warning(f"⚠️ Independent search error for '{kw}': {e}")
            continue

    logger.info(f"🌐 Independent media fetched: {len(all_articles)}")
    return all_articles[:limit]


def build_state_context(articles: List[Dict], max_chars: int = 2500) -> str:
    if not articles:
        return "[STATE MEDIA]: မည်သည့်သတင်းမျှ မတွေ့ရှိပါ။"

    blocks = []
    for i, art in enumerate(articles, 1):
        newspaper = art.get("newspaper_name", "State Media")
        date = art.get("issue_date", "N/A")
        page = art.get("page_no", "N/A")
        headline = art.get("headline", "ခေါင်းစဉ်မရှိ")
        body = (art.get("body_text", "") or "")[:max_chars]
        citation = f"[Source: {newspaper} | {date} | Page {page}]"
        blocks.append(f"--- Article {i} ---\n{citation}\nခေါင်းစဉ်: {headline}\nအကြောင်းအရာ:\n{body}\n")

    return "\n".join(blocks)


def build_independent_context(articles: List[Dict], max_chars: int = 2500) -> str:
    if not articles:
        return "[INDEPENDENT MEDIA]: မည်သည့်သတင်းမျှ မတွေ့ရှိပါ။"

    blocks = []
    for i, art in enumerate(articles, 1):
        source = art.get("source_name", "Independent Media")
        date = art.get("published_date", "N/A")
        url = art.get("url", "")
        headline = art.get("headline", "ခေါင်းစဉ်မရှိ")
        body = (art.get("body_text", "") or "")[:max_chars]
        citation = f"[Source: {source} | Date: {date} | Link: {url}]"
        blocks.append(f"--- Article {i} ---\n{citation}\nခေါင်းစဉ်: {headline}\nအကြောင်းအရာ:\n{body}\n")

    return "\n".join(blocks)


def generate_verification_report(topic: str, state_context: str, independent_context: str) -> str:
    if not genai_client:
        return "❌ Gemini API မရရှိနိုင်သဖြင့် Report မထုတ်နိုင်ပါ။"

    prompt = f"""You are the Chief Fact-Checking Editor of BBC Verify and Reuters Fact Check.
Your task is to write a rigorous 3-Pillar Cross-Source Verification Report in Myanmar.

ခေါင်းစဉ်: "{topic}"

[SOURCE A — STATE MEDIA (မြန်မာ့အလင်း၊ ကြေးမုံ)]
{state_context}

[SOURCE B — INDEPENDENT MEDIA (BBC, Irrawaddy, etc.)]
{independent_context}

INSTRUCTIONS — Follow this Markdown format:

🔍 **Cross-Source Verification Report**
**ခေါင်းစဉ်:** {topic}
**နေ့စွဲ:** {datetime.now().strftime('%Y-%m-%d')}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ **Pillar 1: COMMON GROUND (တူညီသောအချက်များ)**
နှစ်ဖက်စလုံး သဘောတူသော အချက်များကို bullet ဖြင့် ရေးပါ။ Source တစ်ဖက်တည်းသာ ရှိပါက အခြေအနေကို ဖော်ပြပါ။
- [အချက်] _(State: [Citation] | Independent: [Citation])_

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **Pillar 2: DIVERGENCES (ကွဲလွဲချက်များ)**
တူညီသောအကြောင်းအရာအပေါ် ကွဲပြားစွာ ဖော်ပြထားသော အချက်များ သို့မဟုတ် ကိန်းဂဏန်းများ။
- **State Media ဖော်ပြချက်:** [...]
- **Independent ဖော်ပြချက်:** [...]
- **ကွာဟမှု အနှစ်ချုပ်:** [...]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ **Pillar 3: CRITICAL OMISSIONS (ချန်လှပ်ထားချက်များ)**
**State Media မှ ချန်လှပ်ထားချက်များ:** [...]
**Independent Media မှ ချန်လှပ်ထားချက်များ:** [...]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📊 **Editorial Summary (အယ်ဒီတာ့ အနှစ်ချုပ်)**
သမာသမတ်ကျသော ခြုံငုံသုံးသပ်ချက် တစ်ပိုဒ်။

STRICT RULES:
1. Context ထဲတွင် ပါဝင်သော facts များကိုသာ အသုံးပြုပါ။
2. Source တစ်ဖက်တည်းသာ ရရှိပါက "Source တစ်ဖက်တည်းသာ ရရှိသဖြင့် Cross-verification ပြည့်စုံရန် ခက်ခဲပါသည်" ဟု ရှင်းလင်းစွာ ဖော်ပြပါ။
"""
    try:
        cfg = types.GenerateContentConfig(temperature=0.2, max_output_tokens=3500)
        return generate_with_retry(prompt, cfg)
    except Exception as e:
        logger.error(f"❌ Report generation failed: {e}")
        return f"❌ Report ထုတ်ရာတွင် အမှားရှိပါသည်: {str(e)}"


def run_cross_source_comparison(topic: str) -> Dict[str, Any]:
    logger.info(f"🎯 Starting cross-source comparison for: '{topic}'")

    if not genai_client:
        return {
            "success": False,
            "report": "❌ Gemini API Client ချိတ်ဆက်ထားခြင်း မရှိပါ။",
            "state_count": 0,
            "independent_count": 0,
            "keywords": []
        }

    keywords = plan_comparison_keywords(topic)
    if not keywords:
        return {
            "success": False,
            "report": "❌ သတင်းရှာဖွေရန် Keyword မထုတ်ယူနိုင်ပါ။",
            "state_count": 0,
            "independent_count": 0,
            "keywords": []
        }

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_state = executor.submit(fetch_state_media_articles, keywords)
        future_indep = executor.submit(fetch_independent_media_articles, keywords)

        state_articles = future_state.result()
        independent_articles = future_indep.result()

    if not state_articles and not independent_articles:
        return {
            "success": False,
            "report": (
                f"⚠️ **သတင်းအချက်အလက် မတွေ့ရှိပါ**\n\n"
                f"'{topic}' နှင့်ပတ်သက်ပြီး Database နှစ်ဖက်စလုံးတွင် မတွေ့ရှိပါ။\n\n"
                f"🔑 အသုံးပြုခဲ့သည့် Keywords: {', '.join(keywords)}"
            ),
            "state_count": 0,
            "independent_count": 0,
            "keywords": keywords
        }

    state_context = build_state_context(state_articles)
    independent_context = build_independent_context(independent_articles)

    report = generate_verification_report(topic, state_context, independent_context)

    footer = (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 **Data Metrics:**\n"
        f"• State Media Articles: {len(state_articles)}\n"
        f"• Independent Articles: {len(independent_articles)}\n"
        f"• Search Keywords: {', '.join(keywords)}\n"
        f"• Search Window: {SEARCH_WINDOW_DAYS} days\n"
        f"• Model: {GEMINI_MODEL}"
    )

    return {
        "success": True,
        "report": report + footer,
        "state_count": len(state_articles),
        "independent_count": len(independent_articles),
        "keywords": keywords
    }