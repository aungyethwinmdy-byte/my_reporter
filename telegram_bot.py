"""
================================================================
MYANMAR INTELLIGENT NEWSROOM BOT (v7.3 Stable Edition)
- Multi-Route Intelligence:
  1. Greeting / casual filter
  2. System meta & sources queries (/sources, /status)
  3. Precision numeric & commodity engine (cards dashboard)
  4. General newspaper article search & editorial
  5. General AI conversation fallback
- Lazy client initialization (safe imports in CI/tests)
- Shared Gemini models & fallback chain from gemini_config
================================================================
"""

import os
import re
import time
import json
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

# Optional dotenv loading
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client, Client
from google import genai
from google.genai import types

import gemini_config
from gemini_config import generate_content_with_fallback

from telegram import Update
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Custom Modules
from report_formatter import (
    format_numeric_dashboard,
    clean_number_value,
    normalize_digits,
    to_burmese_digits,
    simplify_item_name,
)
from system_router import (
    is_greeting_or_casual,
    get_greeting_response,
    is_system_meta_query,
    get_sources_report,
    get_system_status,
    handle_general_ai_conversation,
)

# Backward-compatible alias for tests and external callers
build_precision_numeric_report = format_numeric_dashboard

__all__ = [
    "build_precision_numeric_report",
    "clean_number_value",
    "detect_commodity_context",
    "execute_articles_search",
    "get_myanmar_dates",
    "is_numeric_or_price_query",
    "normalize_digits",
    "split_message_text",
    "to_burmese_digits",
    "simplify_item_name",
    "GEMINI_MODEL",
    "GEMINI_MODELS",
]

# Optional Cross-Source Verifier
try:
    from cross_source_verifier import run_cross_source_comparison
except ImportError:
    run_cross_source_comparison = None


# ============================================================
# 1. CONFIGURATION & CLIENT MANAGEMENT (STABLE LAZY INIT)
# ============================================================

MYANMAR_TZ = ZoneInfo("Asia/Yangon")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

# Shared model defaults + fallback chain (GEMINI_MODEL / GEMINI_FALLBACK_MODELS)
GEMINI_MODEL = gemini_config.GEMINI_MODEL
GEMINI_MODELS = gemini_config.GEMINI_MODELS

NEWSPAPERS = ["မြန်မာ့အလင်း", "ကြေးမုံ"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NewsroomBot")

_supabase_client: Optional[Client] = None
_genai_client: Optional[genai.Client] = None


def get_supabase_client() -> Optional[Client]:
    """Standard Lazy getter for Supabase Client."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def get_genai_client() -> Optional[genai.Client]:
    """Lazy getter for Gemini Client."""
    global _genai_client
    if _genai_client is None and GEMINI_API_KEY:
        try:
            _genai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            logger.error("GenAI client init error: %s", e)
    return _genai_client


# ============================================================
# 2. DATE & TELEGRAM HELPERS
# ============================================================

def get_myanmar_dates() -> tuple[str, str]:
    """Always return today and yesterday in Myanmar Time (Asia/Yangon)."""
    now_mm = datetime.now(MYANMAR_TZ)
    today_str = now_mm.strftime("%Y-%m-%d")
    yesterday_str = (now_mm - timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, yesterday_str


async def safe_send_or_edit(
    message_obj,
    text: str,
    is_edit: bool = False,
    update_context=None,
):
    """Send or edit telegram messages with retry and plain text fallback."""
    clean_text = text.replace("*", "").replace("_", "").replace("`", "")
    for attempt in range(3):
        try:
            if is_edit and message_obj:
                return await message_obj.edit_text(text, parse_mode="Markdown")
            elif update_context and update_context.message:
                return await update_context.message.reply_text(text, parse_mode="Markdown")
            return None
        except (TimedOut, NetworkError) as e:
            await asyncio.sleep(1)
            if attempt < 2:
                continue
            logger.warning("⚠️ Network timeout during delivery: %s", e)
        except Exception as e:
            logger.warning("Markdown parse failed, fallback to plain text: %s", e)
            try:
                if is_edit and message_obj:
                    return await message_obj.edit_text(clean_text)
                elif update_context and update_context.message:
                    return await update_context.message.reply_text(clean_text)
            except Exception as final_e:
                logger.error("Failed to send message completely: %s", final_e)
            return None
    return None


def split_message_text(text: str, max_length: int = 3500) -> list[str]:
    """Telegram 4096 character limit ကို ကျော်လွန်ခြင်း မရှိစေရန် ခွဲထုတ်ခြင်း"""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current_chunk = ""
    for paragraph in text.split("\n\n"):
        if len(current_chunk) + len(paragraph) + 2 <= max_length:
            current_chunk += (paragraph + "\n\n")
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            if len(paragraph) > max_length:
                for i in range(0, len(paragraph), max_length):
                    chunks.append(paragraph[i:i + max_length])
                current_chunk = ""
            else:
                current_chunk = paragraph + "\n\n"

    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


# ============================================================
# 3. NUMERIC & COMMODITY DATABASE ENGINE
# ============================================================

def is_numeric_or_price_query(query: str) -> bool:
    """စက်သုံးဆီ၊ ရွှေ၊ ငွေလဲနှုန်း စသည့် ကိန်းဂဏန်းသီးသန့် မေးမြန်းချက် ဟုတ်/မဟုတ် စစ်ဆေးခြင်း"""
    keywords = [
        "စက်သုံးဆီ", "ဓာတ်ဆီ", "ဒီဇယ်", "octane", "diesel",
        "ရွှေ", "ရွှေဈေး", "ဒေါ်လာ", "ငွေလဲနှုန်း", "usd",
        "ဈေး", "ဈေးနှုန်း", "ပေါက်ဈေး", "ရည်ညွှန်း", "ကျပ်",
        "မနေ့က", "ဒီနေ့", "နှိုင်းယှဉ်", "တက်", "ကျ", "အပြောင်းအလဲ",
    ]
    low = query.lower()
    return any(k in low for k in keywords)


def detect_commodity_context(query: str) -> str:
    """မေးခွန်းအတွင်းမှ အဓိက ကုန်စည်အမျိုးအမည်ကို သတ်မှတ်ခြင်း"""
    low = query.lower()
    if any(k in low for k in ["စက်သုံးဆီ", "ဓာတ်ဆီ", "ဒီဇယ်", "ဒဇယ်", "octane", "diesel", "ဆီ"]):
        return "စက်သုံးဆီ"
    if "ရွှေ" in low:
        return "ရွှေ"
    if any(k in low for k in ["ဒေါ်လာ", "ငွေလဲ", "usd"]):
        return "ဒေါ်လာ"
    if any(k in low for k in ["စပါး", "ဆန်"]):
        return "စပါး"
    if any(k in low for k in ["ပဲ", "မတ်ပဲ", "ပဲတီစိမ်း"]):
        return "ပဲ"
    return ""


def query_newspaper_numbers(
    supabase_client: Client,
    search_term: str,
    dates: list[str],
) -> list[dict]:
    """Supabase `newspaper_numbers` ဇယားမှ သတ်မှတ်ရက်စွဲအလိုက် ကိန်းဂဏန်းများ ဆွဲထုတ်ခြင်း (with Retry)"""
    if not supabase_client:
        return []
    for attempt in range(2):
        try:
            query = (
                supabase_client.from_("newspaper_numbers")
                .select("publication_date, headline, section, context, value, original_value, unit, source_text")
                .in_("publication_date", dates)
            )
            if search_term:
                clean_term = re.sub(r"[,;'\"()%]", "", search_term).strip()
                if clean_term:
                    query = query.or_(
                        f"context.ilike.%{clean_term}%,headline.ilike.%{clean_term}%,section.ilike.%{clean_term}%"
                    )
            res = query.order("publication_date", desc=False).execute()
            return res.data or []
        except Exception as e:
            logger.warning("Query newspaper_numbers attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                time.sleep(1)
    return []


# ============================================================
# 4. AUTO-CACHE EXTRACTOR (FALLBACK)
# ============================================================

def extract_and_cache_missing_numbers(
    client: genai.Client,
    supabase_client: Client,
    retrieved_articles: list[dict],
    dates: list[str],
    commodity_keyword: str,
) -> list[dict]:
    """
    If `newspaper_numbers` does not have numbers yet, extract them from
    retrieved articles and auto-save into `newspaper_numbers` for future queries!
    """
    if not client or not retrieved_articles or not supabase_client:
        return []

    context_snippets = []
    for art in retrieved_articles:
        dt = str(art.get("issue_date"))
        if dt in dates:
            context_snippets.append(
                f"[Date: {dt} | Page: {art.get('page_no')} | Headline: {art.get('headline')}]\n"
                f"{art.get('body_text')}"
            )

    joined_text = "\n\n---\n\n".join(context_snippets[:6])
    if not joined_text.strip():
        return []

    prompt = f"""
You are an expert financial and commodity extraction engine for Myanmar newspapers.
Extract all specific prices, rates, yields, or metrics for: {commodity_keyword or 'all commodities'}

DATES TO EXTRACT: {dates}

TEXT EVIDENCE:
{joined_text}

Return a valid JSON array of objects:
[
  {{
    "publication_date": "YYYY-MM-DD",
    "headline": "headline",
    "section": "category",
    "context": "item name (e.g. Octane 92 ရည်ညွှန်းလက်ကားဈေး)",
    "value": "plain english number (e.g. 3050)",
    "original_value": "burmese number as written (e.g. ၃,၀၅၀)",
    "unit": "ကျပ် or unit",
    "source_text": "verbatim short snippet"
  }}
]

If no clear prices/numbers are found, return [].
"""
    try:
        def _parse_json(text):
            cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
            return json.loads(cleaned)

        items = generate_content_with_fallback(
            client,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
            parse=_parse_json,
        )
        if isinstance(items, list) and items:
            for it in items:
                it["value"] = str(clean_number_value(it.get("value")) or it.get("value"))
            try:
                supabase_client.from_("newspaper_numbers").insert(items).execute()
                logger.info("💾 Auto-cached %d new numeric items into DB.", len(items))
            except Exception as e:
                logger.warning("Auto-cache insert failed: %s", e)
            return items
    except Exception as e:
        logger.error("Auto extraction error: %s", e)

    return []


# ============================================================
# 5. GENERAL ARTICLE SEARCH (STANDARD ENGINE)
# ============================================================

def execute_articles_search(
    supabase_client: Client,
    search_terms: list[str],
    dates: list[str],
) -> list[dict]:
    """General text search across `articles` view (Page 2 first)."""
    if not supabase_client:
        return []
    all_rows = []
    seen = set()

    for dt in dates:
        for paper in NEWSPAPERS:
            # Always get Page 2 first
            try:
                p2 = (
                    supabase_client.from_("articles")
                    .select("article_id, newspaper_name, issue_date, page_no, headline, body_text")
                    .eq("issue_date", dt)
                    .ilike("newspaper_name", f"%{paper}%")
                    .eq("page_no", 2)
                    .limit(20)
                    .execute()
                )
                for r in p2.data or []:
                    aid = r.get("article_id")
                    if aid not in seen:
                        seen.add(aid)
                        all_rows.append(r)
            except Exception as e:
                logger.warning("Page 2 query error: %s", e)

            # Keyword query
            if search_terms:
                clean_kws = [re.sub(r"[,;'\"()%]", "", kw).strip() for kw in search_terms[:5]]
                clean_kws = [k for k in clean_kws if k]
                if clean_kws:
                    or_clauses = [
                        f"headline.ilike.%{kw}%,body_text.ilike.%{kw}%"
                        for kw in clean_kws
                    ]
                    try:
                        res = (
                            supabase_client.from_("articles")
                            .select("article_id, newspaper_name, issue_date, page_no, headline, body_text")
                            .eq("issue_date", dt)
                            .ilike("newspaper_name", f"%{paper}%")
                            .or_(",".join(or_clauses))
                            .limit(10)
                            .execute()
                        )
                        for r in res.data or []:
                            aid = r.get("article_id")
                            if aid not in seen:
                                seen.add(aid)
                                all_rows.append(r)
                    except Exception as e:
                        logger.warning("Keyword query error: %s", e)

    return all_rows


def generate_general_editorial_response(
    client: genai.Client,
    user_query: str,
    retrieved_articles: list[dict],
    dates: list[str],
) -> str:
    """Fallback editorial response for non-numeric narrative queries."""
    if not retrieved_articles:
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📰 **သတင်းမီဒီယာ စောင့်ကြည့်သုံးသပ်ချက်**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 မေးမြန်းချက်: {user_query}\n\n"
            f"⚠️ ဒေတာဘေ့စ်တွင် {' / '.join(dates)} အတွက် သက်ဆိုင်ရာမှတ်တမ်း မတွေ့ရှိပါ။\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    context_blocks = []
    for r in retrieved_articles[:12]:
        context_blocks.append(
            f"• [{r.get('newspaper_name')} | စာမျက်နှာ {r.get('page_no')} | {r.get('issue_date')}]\n"
            f"ခေါင်းစဉ်: {r.get('headline')}\n"
            f"အကြောင်းအရာ: {str(r.get('body_text'))[:1500]}"
        )

    prompt = f"""
You are the Executive Editor of Myanmar Intelligent Newsroom.
Answer the user's question using ONLY the provided newspaper evidence.
Do not invent facts, figures, or dates.

User Question: {user_query}
Dates: {dates}

DATABASE EVIDENCE:
{chr(10).join(context_blocks)}

Provide a concise, professional Myanmar summary with newspaper citations.
"""
    try:
        res = generate_content_with_fallback(client, contents=prompt)
        return (res.text or "").strip()
    except Exception as e:
        return f"⚠️ အယ်ဒီတာ့ သုံးသပ်ချက် ထုတ်ပြန်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: {e}"


# ============================================================
# 6. COMMAND HANDLERS
# ============================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start and /help command handler"""
    welcome = (
        "🇲🇲 **Myanmar Intelligent Newsroom AI Bot (v7.3)**\n\n"
        "• နေ့စဉ်ထုတ် သတင်းစာများမှ **စက်သုံးဆီ၊ ရွှေ၊ ငွေလဲနှုန်းနှင့် ကုန်ဈေးနှုန်းများကို ၁၀၀% တိကျစွာ** စိစစ်တွက်ချက်ပေးပါသည်\n"
        "• မနေ့ကနှင့် ဒီနေ့ ဈေးနှုန်းကွာခြားချက် (တက်/ကျ) များကို သင်္ချာနည်းကျ အတိအကျ ပြသပေးပါသည်\n\n"
        "📌 **စမ်းသပ် မေးမြန်းနိုင်သော ဥပမာများ:**\n"
        "• `မနေ့က စက်သုံးဆီဈေးနှုန်းအခြေအနေနဲ့ ဒီနေ့ စက်သုံးဆီအခြေအနေ နှိုင်းယှဉ်ပြပါ`\n"
        "• `ဒီနေ့ ရွှေရည်ညွှန်းဈေး ဘယ်လောက်လဲ`\n"
        "• `/compare <ခေါင်းစဉ်>` (သတင်းဌာနစုံ Cross-Check ပြုလုပ်ရန်)\n"
        "• `/sources` (စောင့်ကြည့်သော မီဒီယာများ စာရင်း)\n"
        "• `/status` (ဒေတာဘေ့စ် လက်ရှိအခြေအနေ)"
    )
    await safe_send_or_edit(None, welcome, is_edit=False, update_context=update)


async def sources_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sources command: စနစ်အတွင်း ချိတ်ဆက်ထားသော သတင်းဌာနများ စာရင်း ပြသခြင်း"""
    text = get_sources_report()
    await update.message.reply_text(text, parse_mode="Markdown")


async def status_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status command: ဒေတာဘေ့စ် Status ပြသခြင်း"""
    supabase = get_supabase_client()
    text = get_system_status(supabase)
    await update.message.reply_text(text, parse_mode="Markdown")


async def compare_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/compare command: သတင်းဌာနစုံ Cross-Source စိစစ်ခြင်း"""
    if not context.args:
        await update.message.reply_text(
            "📋 **/compare အသုံးပြုနည်း:** `/compare <ခေါင်းစဉ်>`\nဥပမာ: `/compare စစ်ရေး`",
            parse_mode="Markdown",
        )
        return

    if not run_cross_source_comparison:
        await update.message.reply_text("⚠️ Cross Source Verifier Module မရှိပါ။", parse_mode="Markdown")
        return

    topic = " ".join(context.args).strip()
    status_msg = await safe_send_or_edit(
        None,
        f"🔍 **Cross-Source Fact-Check စတင်နေပါသည်...**\nခေါင်းစဉ်: _{topic}_",
        update_context=update,
    )

    try:
        report = await asyncio.to_thread(run_cross_source_comparison, topic)
        text = report.get("report") or report.get("editorial") or "စိစစ်ချက် မတွေ့ရှိပါ။"
        chunks = split_message_text(text)
        await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
        for ch in chunks[1:]:
            await safe_send_or_edit(None, ch, is_edit=False, update_context=update)
    except Exception as e:
        logger.error("Compare error: %s", e)
        await safe_send_or_edit(status_msg, f"⚠️ အမှားဖြစ်ပေါ်ခဲ့သည်: `{e}`", is_edit=True)


# ============================================================
# 7. MAIN MESSAGE HANDLER (MULTI-ROUTE INTELLIGENCE)
# ============================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_query = update.message.text.strip()
    if not user_query:
        return

    # ----------------------------------------------------
    # ROUTE 1: GREETING & CASUAL FILTER
    # ----------------------------------------------------
    if is_greeting_or_casual(user_query):
        welcome_reply = get_greeting_response()
        await update.message.reply_text(welcome_reply, parse_mode="Markdown")
        return

    # ----------------------------------------------------
    # ROUTE 2: SYSTEM META & SOURCES QUERIES
    # ----------------------------------------------------
    if is_system_meta_query(user_query):
        report = get_sources_report()
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    today_str, yesterday_str = get_myanmar_dates()

    # Determine date scope
    if any(k in user_query for k in ["မနေ့", "ယမန်နေ့", "နှိုင်းယှဉ်", "နှိင်းယှဉ်", "ယှဉ်ပြ", "ကွာခြား", "ပြောင်းလဲ"]):
        target_dates = [yesterday_str, today_str]
    elif "မနေ့" in user_query:
        target_dates = [yesterday_str]
    else:
        target_dates = [today_str]

    status_msg = await safe_send_or_edit(
        None,
        "🔍 သတင်းစာ ဒေတာဘေ့စ်တွင် စိစစ်ရှာဖွေနေပါသည်...",
        update_context=update,
    )

    try:
        supabase = get_supabase_client()
        genai_client = get_genai_client()

        # ----------------------------------------------------
        # ROUTE 3: PRECISION NUMERIC & COMMODITY ROUTE
        # ----------------------------------------------------
        if is_numeric_or_price_query(user_query):
            comm_key = detect_commodity_context(user_query)
            logger.info("🎯 Numeric Route: '%s'", comm_key)

            numeric_records = await asyncio.to_thread(
                query_newspaper_numbers, supabase, comm_key, target_dates
            )

            # Auto-cache fallback if empty
            if not numeric_records and genai_client:
                logger.info("ℹ️ Numbers missing in cache. Extracting from articles...")
                articles = await asyncio.to_thread(
                    execute_articles_search, supabase, [comm_key or "ရည်ညွှန်း"], target_dates
                )
                numeric_records = await asyncio.to_thread(
                    extract_and_cache_missing_numbers, genai_client, supabase, articles, target_dates, comm_key
                )

            if numeric_records:
                report = format_numeric_dashboard(
                    data_rows=numeric_records,
                    dates=target_dates,
                    user_query=user_query,
                    title_override=f"{comm_key} ဈေးနှုန်းများ" if comm_key else "",
                )
                chunks = split_message_text(report)
                await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
                for ch in chunks[1:]:
                    await safe_send_or_edit(None, ch, is_edit=False, update_context=update)
                return

        # ----------------------------------------------------
        # ROUTE 4: GENERAL NEWSPAPER ARTICLE SEARCH
        # ----------------------------------------------------
        raw_words = [w for w in user_query.split() if len(w) >= 2][:5]
        search_terms = [re.sub(r"[,;'\"()%]", "", w).strip() for w in raw_words]
        search_terms = [w for w in search_terms if w]

        articles = await asyncio.to_thread(execute_articles_search, supabase, search_terms, target_dates)

        if articles:
            final_answer = await asyncio.to_thread(
                generate_general_editorial_response, genai_client, user_query, articles, target_dates
            )
            chunks = split_message_text(final_answer)
            await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
            for ch in chunks[1:]:
                await safe_send_or_edit(None, ch, is_edit=False, update_context=update)
            return

        # ----------------------------------------------------
        # ROUTE 5: GENERAL AI CONVERSATION (သတင်းစာထဲ မပါသော အထွေထွေမေးခွန်းများ)
        # ----------------------------------------------------
        general_answer = await asyncio.to_thread(
            handle_general_ai_conversation, user_query, genai_client, GEMINI_MODEL
        )
        await safe_send_or_edit(status_msg, general_answer, is_edit=True)

    except Exception as e:
        logger.error("❌ Execution error: %s", e, exc_info=True)
        await safe_send_or_edit(status_msg, f"⚠️ အချက်အလက်ထုတ်ယူရာတွင် ချို့ယွင်းချက်ဖြစ်ပေါ်ခဲ့သည်: `{e}`", is_edit=True)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning("⚠️ Network fluctuation: %s", context.error)
        return
    logger.error("❌ Update error: %s", context.error, exc_info=context.error)


# ============================================================
# 8. MAIN ENTRYPOINT
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("❌ TELEGRAM_BOT_TOKEN မရှိပါ။ .env ကို စစ်ဆေးပါ။")

    print("🤖 Myanmar Intelligent Newsroom Bot (v7.3 Stable) is starting...")
    print("🕘 Timezone: Asia/Yangon")
    print(f"📰 Monitored Papers: {', '.join(NEWSPAPERS)}")
    print(f"🧠 Gemini models: {gemini_config.describe_models()}")

    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        pool_timeout=30.0,
    )

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()

    # Command Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("sources", sources_command_handler))
    app.add_handler(CommandHandler("status", status_command_handler))
    app.add_handler(CommandHandler("compare", compare_command_handler))

    # Message Handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot polling is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
