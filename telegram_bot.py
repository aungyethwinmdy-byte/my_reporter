"""
================================================================
MYANMAR INTELLIGENT NEWSROOM BOT (v7.3 Stable Edition)
- Fixed: Removed ClientOptions incompatibility with supabase-py
- Greeting Filter: Clean greetings without unwanted news dumps
- Precision Numeric Engine: Exact fuel & gold price comparisons
- Custom Commands: /start, /help, /sources, /status, /compare
- Lazy Client Init: CI tests will never crash on import
================================================================
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types

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
from report_formatter import format_numeric_dashboard, clean_number_value
from system_router import (
    is_greeting_or_casual,
    get_greeting_response,
    is_system_meta_query,
    get_sources_report,
    get_system_status,
    handle_general_ai_conversation,
)

# Optional Cross-Source Verifier
try:
    from cross_source_verifier import run_cross_source_comparison
except ImportError:
    run_cross_source_comparison = None


# ============================================================
# 1. CONFIGURATION & CLIENT MANAGEMENT (STABLE LAZY INIT)
# ============================================================

MYANMAR_TZ = ZoneInfo("Asia/Yangon")

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    or os.getenv("SUPABASE_KEY")
    or os.getenv("SUPABASE_ANON_KEY")
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

NEWSPAPERS = ["မြန်မာ့အလင်း", "ကြေးမုံ"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NewsroomBot")

_supabase_client: Client = None
_genai_client = None


def get_supabase_client() -> Client:
    """Standard Lazy getter for Supabase Client."""
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("❌ Supabase Credentials မပြည့်စုံပါ။ .env ကို စစ်ဆေးပါ။")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def get_genai_client():
    """Lazy getter for Gemini Client."""
    global _genai_client
    if _genai_client is None and GEMINI_API_KEY:
        try:
            _genai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as exc:
            logger.error("❌ Gemini init failed: %s", exc)
    return _genai_client


# ============================================================
# 2. DATE & TELEGRAM HELPERS
# ============================================================

def get_myanmar_dates() -> tuple[str, str]:
    """Always calculate today and yesterday in Myanmar Time."""
    now_mm = datetime.now(MYANMAR_TZ)
    today_str = now_mm.strftime("%Y-%m-%d")
    yesterday_str = (now_mm - timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, yesterday_str


async def safe_send_or_edit(message_obj, text: str, is_edit: bool = False, update_context=None):
    """Send or edit telegram messages with retry and plain text fallback."""
    clean_text = text.replace("*", "").replace("_", "").replace("`", "")
    for attempt in range(3):
        try:
            if is_edit and message_obj:
                return await message_obj.edit_text(text, parse_mode="Markdown")
            if update_context and update_context.message:
                return await update_context.message.reply_text(text, parse_mode="Markdown")
        except (TimedOut, NetworkError) as e:
            if attempt < 2:
                await asyncio.sleep(2)
                continue
            logger.warning("⚠️ Network timeout during delivery: %s", e)
        except Exception as e:
            logger.warning("Markdown parse failed, retrying plain text: %s", e)
            try:
                if is_edit and message_obj:
                    return await message_obj.edit_text(clean_text)
                if update_context and update_context.message:
                    return await update_context.message.reply_text(clean_text)
            except Exception as final_err:
                logger.error("❌ Plain text delivery failed: %s", final_err)
                return None
    return None


def split_message_text(text: str, max_length: int = 3500) -> list[str]:
    """Split text into telegram-safe chunks."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            for s in range(0, len(p), max_length):
                chunks.append(p[s:s + max_length].strip())
            continue

        proposed = f"{current_chunk}\n\n{p}" if current_chunk else p
        if len(proposed) > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
            current_chunk = p
        else:
            current_chunk = proposed

    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks


# ============================================================
# 3. NUMERIC & COMMODITY DATABASE ENGINE
# ============================================================

def is_numeric_or_price_query(query: str) -> bool:
    """စစ်ဆေးချက်: မေးခွန်းသည် ဈေးနှုန်း၊ ကိန်းဂဏန်း၊ နှိုင်းယှဉ်ချက် ဟုတ်/မဟုတ်"""
    markers = [
        "ဈေး", "စျေး", "ရည်ညွှန်း", "ကိန်းဂဏန်း", "ဂဏန်း", "ဘယ်လောက်",
        "နှုန်း", "တက်", "ကျ", "တိုး", "လျော့", "အပြောင်းအလဲ", "နှိုင်းယှဉ်",
        "နှိင်းယှဉ်", "ယှဉ်ပြ", "ကွာခြား", "octane", "diesel", "ဒီဇယ်", "ဒဇယ်",
        "ဓာတ်ဆီ", "စက်သုံးဆီ", "ရွှေ", "ဒေါ်လာ", "စားအုန်းဆီ", "စပါး", "အထွက်နှုန်း"
    ]
    low = query.lower()
    return any(m in low for m in markers)


def detect_commodity_context(query: str) -> str:
    """မေးခွန်းအတွင်းမှ အဓိက ကုန်စည်အမျိုးအမည်ကို သတ်မှတ်ခြင်း"""
    low = query.lower()
    if any(k in low for k in ["စက်သုံးဆီ", "ဓာတ်ဆီ", "ဒီဇယ်", "ဒဇယ်", "octane", "diesel", "ဆီ"]):
        return "စက်သုံးဆီ"
    if "ရွှေ" in low:
        return "ရွှေ"
    if any(k in low for k in ["ဒေါ်လာ", "ငွေလဲနှုန်း", "usd", "fx"]):
        return "ဒေါ်လာ"
    if "စားအုန်းဆီ" in low:
        return "စားအုန်းဆီ"
    if any(k in low for k in ["စပါး", "ဆန်", "မိုးစပါး"]):
        return "စပါး"
    return ""


def query_newspaper_numbers(supabase_client: Client, search_term: str, dates: list[str]) -> list[dict]:
    """Supabase `newspaper_numbers` ဇယားမှ သတ်မှတ်ရက်စွဲအလိုက် ကိန်းဂဏန်းများ ဆွဲထုတ်ခြင်း (with Retry)"""
    for attempt in range(2):
        try:
            query = (
                supabase_client.from_("newspaper_numbers")
                .select("publication_date, headline, section, context, value, original_value, unit, source_text")
                .in_("publication_date", dates)
            )
            if search_term:
                query = query.or_(
                    f"context.ilike.%{search_term}%,headline.ilike.%{search_term}%,section.ilike.%{search_term}%"
                )
            res = query.order("publication_date", desc=False).execute()
            return res.data or []
        except Exception as e:
            logger.warning("Query attempt %d for numbers failed: %s", attempt + 1, e)
            if attempt == 0:
                import time
                time.sleep(2)
                continue
    return []


def extract_and_cache_missing_numbers(client, supabase_client: Client, retrieved_articles: list[dict], dates: list[str], commodity_keyword: str) -> list[dict]:
    """Auto-cache numbers into `newspaper_numbers` if not found in table."""
    if not client or not retrieved_articles:
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
DATES: {dates}
EVIDENCE:
{joined_text}

Return ONLY a valid JSON array of objects:
[
  {{
    "publication_date": "YYYY-MM-DD",
    "headline": "headline",
    "section": "category",
    "context": "item name (e.g. Octane 92 ရည်ညွှန်းလက်ကားဈေး)",
    "value": "plain english number (e.g. 3050)",
    "original_value": "burmese number as written (e.g. ၃,၀၅၀)",
    "unit": "ကျပ် or unit",
    "source_text": "verbatim snippet"
  }}
]
If none, return [].
"""
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json"),
        )
        items = json.loads(response.text)
        if isinstance(items, list) and items:
            for it in items:
                it["value"] = str(clean_number_value(it.get("value")) or it.get("value"))
            try:
                supabase_client.from_("newspaper_numbers").insert(items).execute()
                logger.info("💾 Auto-cached %d items into newspaper_numbers.", len(items))
            except Exception as e:
                logger.warning("Auto-cache insert failed: %s", e)
            return items
    except Exception as e:
        logger.error("Auto extraction error: %s", e)

    return []


# ============================================================
# 4. ARTICLE SEARCH & EDITORIAL RESPONSE
# ============================================================

def execute_articles_search(supabase_client: Client, search_terms: list[str], dates: list[str]) -> list[dict]:
    """Search articles strictly matching query terms across dates."""
    if not search_terms:
        return []

    all_rows = []
    seen = set()
    clean_terms = [re.sub(r"[,;\'\"()%]", "", kw).strip() for kw in search_terms if len(kw.strip()) >= 2]

    if not clean_terms:
        return []

    or_clauses = [f"headline.ilike.%{kw}%,body_text.ilike.%{kw}%" for kw in clean_terms[:5]]

    for dt in dates:
        for paper in NEWSPAPERS:
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
                logger.warning("Keyword query error for %s / %s: %s", paper, dt, e)

    return all_rows


def generate_general_editorial_response(client, user_query: str, retrieved_articles: list[dict], dates: list[str]) -> str:
    """Editorial response using verified database context."""
    if not retrieved_articles:
        return (
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📰 **သတင်းမီဒီယာ စောင့်ကြည့်သုံးသပ်ချက်**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 မေးမြန်းချက်: {user_query}\n\n"
            f"⚠️ ဒေတာဘေ့စ်တွင် {' / '.join(dates)} အတွက် သက်ဆိုင်ရာသတင်း မတွေ့ရှိပါ။\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    context_blocks = []
    for r in retrieved_articles[:10]:
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

Provide a concise, professional Myanmar summary with exact citations (newspaper name, page, date).
"""
    try:
        res = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return (res.text or "").strip()
    except Exception as e:
        return f"⚠️ အယ်ဒီတာ့ သုံးသပ်ချက် ထုတ်ပြန်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: {e}"


# ============================================================
# 5. TELEGRAM COMMAND HANDLERS
# ============================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = get_greeting_response()
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def sources_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sources command: စနစ်ထဲရှိ မီဒီယာစာရင်း ပြသခြင်း"""
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
        result = await asyncio.to_thread(run_cross_source_comparison, topic)
        report = result.get("report", "⚠️ Report မထွက်ရှိပါ။")
        chunks = split_message_text(report)
        await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
        for ch in chunks[1:]:
            await safe_send_or_edit(None, ch, is_edit=False, update_context=update)
    except Exception as e:
        await safe_send_or_edit(status_msg, f"⚠️ အမှားဖြစ်ပေါ်ခဲ့သည်: `{e}`", is_edit=True)


# ============================================================
# 6. MAIN MESSAGE HANDLER (MULTI-ROUTE INTELLIGENCE)
# ============================================================

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_query = update.message.text.strip()
    if not user_query:
        return

    # ----------------------------------------------------
    # ROUTE 1: GREETING & CASUAL FILTER (နှုတ်ဆက်စကား စစ်ဆေးခြင်း)
    # ----------------------------------------------------
    if is_greeting_or_casual(user_query):
        welcome_reply = get_greeting_response()
        await update.message.reply_text(welcome_reply, parse_mode="Markdown")
        return

    # ----------------------------------------------------
    # ROUTE 2: SYSTEM META & SOURCES QUERIES ("သတင်းဌာန ဘယ်နှစ်ခုလဲ")
    # ----------------------------------------------------
    if is_system_meta_query(user_query):
        report = get_sources_report()
        await update.message.reply_text(report, parse_mode="Markdown")
        return

    today_str, yesterday_str = get_myanmar_dates()

    # Determine date scope
    if any(k in user_query for k in ["မနေ့", "နှိုင်းယှဉ်", "နှိင်းယှဉ်", "ယှဉ်ပြ", "ကွာခြား", "ပြောင်းလဲ"]):
        target_dates = [yesterday_str, today_str]
    elif "မနေ့" in user_query:
        target_dates = [yesterday_str]
    else:
        target_dates = [today_str]

    status_msg = await safe_send_or_edit(None, "🔍 သတင်းစာ ဒေတာဘေ့စ်တွင် စိစစ်ရှာဖွေနေပါသည်...", update_context=update)

    try:
        supabase = get_supabase_client()
        genai_client = get_genai_client()

        # ----------------------------------------------------
        # ROUTE 3: PRECISION NUMERIC & COMMODITY ROUTE (စက်သုံးဆီ / ရွှေဈေး)
        # ----------------------------------------------------
        if is_numeric_or_price_query(user_query):
            comm_key = detect_commodity_context(user_query)
            logger.info("🎯 Numeric Route: '%s'", comm_key)

            numeric_records = await asyncio.to_thread(query_newspaper_numbers, supabase, comm_key, target_dates)

            # Auto-cache fallback if empty
            if not numeric_records and genai_client:
                logger.info("ℹ️ Numbers missing in cache. Extracting from articles...")
                articles = await asyncio.to_thread(execute_articles_search, supabase, [comm_key or "ရည်ညွှန်း"], target_dates)
                numeric_records = await asyncio.to_thread(
                    extract_and_cache_missing_numbers, genai_client, supabase, articles, target_dates, comm_key
                )

            if numeric_records:
                report = format_numeric_dashboard(
                    data_rows=numeric_records,
                    dates=target_dates,
                    user_query=user_query,
                    title_override=f"{comm_key} ဈေးနှုန်းနှင့် ကိန်းဂဏန်းများ" if comm_key else "",
                )
                chunks = split_message_text(report)
                await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
                for ch in chunks[1:]:
                    await safe_send_or_edit(None, ch, is_edit=False, update_context=update)
                return

        # ----------------------------------------------------
        # ROUTE 4: GENERAL NEWSPAPER ARTICLE SEARCH
        # ----------------------------------------------------
        search_terms = [w for w in user_query.split() if len(w) >= 2][:5]
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
# 7. MAIN ENTRYPOINT
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("❌ TELEGRAM_BOT_TOKEN မရှိပါ။ .env ကို စစ်ဆေးပါ။")

    print("🤖 Myanmar Intelligent Newsroom Bot (v7.3 Stable) is starting...")
    print(f"🕘 Timezone: Asia/Yangon")
    print(f"📰 Monitored Papers: {', '.join(NEWSPAPERS)}")

    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
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

    print("🚀 Bot is polling for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()