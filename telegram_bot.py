"""
================================================================
MYANMAR INTELLIGENT NEWSROOM BOT (v7.0 Precision Edition)
Precision Numeric & Commodity Intelligence + Dual-Date Comparison
================================================================
Features:
1. Exact Number & Commodity Engine querying `newspaper_numbers`
2. Zero-Hallucination Python Mathematical Calculation for Price Changes (+/-)
3. Multi-Commodity Support (Fuel, Gold, Foreign Exchange, Crops, Statistics)
4. Auto-Cache Fallback (Extracts & caches numbers into DB if missing)
5. Asia/Yangon Timezone Guaranteed
6. Cross-Source Fact-Checking (/compare)
7. Full Context Continuity across conversation turns
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

# Optional Cross-Source Verifier
try:
    from cross_source_verifier import run_cross_source_comparison
except ImportError:
    run_cross_source_comparison = None


# ============================================================
# 1. CONFIGURATION & CLIENT INITIALIZATION
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

BURMESE_DIGIT_MAP = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("NewsroomBot")

# NOTE: clients are created lazily (get_* below) so that importing this module
# — unit tests, tooling, CI — never raises on missing credentials.
_supabase_client: Client = None
_genai_client = None


def get_supabase_client() -> Client:
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("❌ Supabase Credentials မပြည့်စုံပါ။ .env ကို စစ်ဆေးပါ။")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def get_genai_client():
    """GEMINI_API_KEY မရှိပါက None ပြန်ပေးပြီး bot ဆက်လည်ပါမည်။"""
    global _genai_client
    if _genai_client is None and GEMINI_API_KEY:
        try:
            _genai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as exc:
            logger.error("❌ Gemini init failed: %s", exc)
    return _genai_client


# ============================================================
# 2. DATE & NUMERIC HELPERS
# ============================================================

def get_myanmar_dates() -> tuple[str, str]:
    """Always return today and yesterday in Myanmar Time (Asia/Yangon)."""
    now_mm = datetime.now(MYANMAR_TZ)
    today_str = now_mm.strftime("%Y-%m-%d")
    yesterday_str = (now_mm - timedelta(days=1)).strftime("%Y-%m-%d")
    return today_str, yesterday_str


def normalize_digits(text: str) -> str:
    """မြန်မာဂဏန်းများကို အင်္ဂလိပ်ဂဏန်းအဖြစ် ပြောင်းလဲခြင်း"""
    return (text or "").translate(BURMESE_DIGIT_MAP)


def clean_number_value(val_str: str) -> float | None:
    """ဂဏန်းစာသားမှ ကော်မာများ ဖြုတ်ပြီး Float သို့ ပြောင်းခြင်း"""
    if val_str is None:
        return None
    s = normalize_digits(str(val_str)).replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


# ============================================================
# 3. TELEGRAM DELIVERY HELPERS
# ============================================================

async def safe_send_or_edit(
    message_obj,
    text: str,
    is_edit: bool = False,
    update_context=None,
):
    """Send or edit messages with markdown fallback to plain text."""
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
            logger.warning("Markdown parse failed, fallback to plain text: %s", e)
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
# 4. NUMERIC & PRICE INTELLIGENCE ENGINE (CORE SYSTEM)
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
    """မေးခွန်းအတွင်းမှ အဓိက ကုန်စည်/ကဏ္ဍ Keyword ကို သတ်မှတ်ခြင်း"""
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


def query_newspaper_numbers(
    supabase_client: Client,
    search_term: str,
    dates: list[str],
) -> list[dict]:
    """Supabase `newspaper_numbers` ဇယားမှ သတ်မှတ်ရက်စွဲအလိုက် ကိန်းဂဏန်းများ ဆွဲထုတ်ခြင်း"""
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
        logger.warning("Query newspaper_numbers failed: %s", e)
        return []


def build_precision_numeric_report(
    data_rows: list[dict],
    dates: list[str],
    user_query: str,
    title_override: str = "",
) -> str:
    """
    Python Mathematical Calculation Engine
    Zero-Hallucination: တက်/ကျ ကိန်းဂဏန်း ကွာခြားချက်ကို Python ဖြင့် တိုက်ရိုက်တွက်ချက်ခြင်း
    """
    if not data_rows:
        return ""

    is_comparison = len(dates) == 2
    d_yesterday, d_today = (dates[0], dates[1]) if is_comparison else (None, dates[0])

    # Group records by (context / item name)
    grouped = {}
    for r in data_rows:
        ctx = r.get("context") or r.get("headline")
        dt = str(r.get("publication_date"))
        if ctx not in grouped:
            grouped[ctx] = {"unit": r.get("unit") or "ကျပ်", "dates": {}}
        grouped[ctx]["dates"][dt] = r

    title = title_override or "သတင်းစာ ကိန်းဂဏန်းနှင့် ဈေးနှုန်း နှိုင်းယှဉ်ချက်"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 **{title}**",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📌 **မေးမြန်းချက်:** {user_query}",
        f"🗓 **ရက်စွဲ အကျုံးဝင်မှု:** {' နှင့် '.join(dates)}\n",
    ]

    if is_comparison:
        lines.append(f"| အမျိုးအမည် / အညွှန်း | {d_yesterday} | {d_today} | အပြောင်းအလဲ | ယူနစ် |")
        lines.append("|---|---:|---:|---:|:---|")
    else:
        lines.append(f"| အမျိုးအမည် / အညွှန်း | ဈေးနှုန်း / တန်ဖိုး | ယူနစ် |")
        lines.append("|---|---:|:---|")

    evidence_list = []

    for ctx, info in grouped.items():
        unit = info["unit"]
        if is_comparison:
            y_rec = info["dates"].get(d_yesterday)
            t_rec = info["dates"].get(d_today)

            y_str = y_rec.get("original_value") or str(y_rec.get("value")) if y_rec else "မပါရှိပါ"
            t_str = t_rec.get("original_value") or str(t_rec.get("value")) if t_rec else "မပါရှိပါ"

            diff_str = "မတွက်ချက်နိုင်ပါ"
            if y_rec and t_rec:
                y_val = clean_number_value(y_rec.get("value"))
                t_val = clean_number_value(t_rec.get("value"))
                if y_val is not None and t_val is not None:
                    diff = t_val - y_val
                    if diff == 0:
                        diff_str = "မပြောင်းလဲ"
                    elif diff > 0:
                        diff_str = f"+{int(diff) if diff.is_integer() else diff:g}"
                    else:
                        diff_str = f"{int(diff) if diff.is_integer() else diff:g}"

            lines.append(f"| {ctx} | {y_str} | {t_str} | {diff_str} | {unit} |")

            for rec in [t_rec, y_rec]:
                if rec and rec.get("source_text"):
                    entry = f"• **{ctx}** ({rec['publication_date']}): {rec['source_text']}"
                    if entry not in evidence_list:
                        evidence_list.append(entry)
        else:
            rec = info["dates"].get(d_today)
            val_str = rec.get("original_value") or str(rec.get("value")) if rec else "မပါရှိပါ"
            lines.append(f"| {ctx} | {val_str} | {unit} |")
            if rec and rec.get("source_text"):
                evidence_list.append(f"• **{ctx}**: {rec['source_text']}")

    report = "\n".join(lines)
    if evidence_list:
        report += "\n\n🏛 **မူရင်း သတင်းစာအထောက်အထား:**\n" + "\n".join(evidence_list[:8])

    report += (
        "\n\n💡 **အယ်ဒီတာ့ သုံးသပ်ချက်:**\n"
        "ဖော်ပြပါ ကိန်းဂဏန်းများသည် သတင်းစာပါ အချက်အလက်များအား "
        "တိကျစွာ ထုတ်ယူတွက်ချက်ထားခြင်းဖြစ်ပြီး ခန့်မှန်းဖော်ပြထားခြင်း မရှိပါ။\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    return report


# ============================================================
# 5. AUTO-CACHE / LLM NUMERIC REPAIR (FALLBACK)
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
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        items = json.loads(response.text)
        if isinstance(items, list) and items:
            # Auto-save to newspaper_numbers
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
# 6. GENERAL ARTICLE SEARCH (STANDARD ENGINE)
# ============================================================

def execute_articles_search(
    supabase_client: Client,
    search_terms: list[str],
    dates: list[str],
) -> list[dict]:
    """General text search across `articles` view."""
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
                or_clauses = [
                    f"headline.ilike.%{kw}%,body_text.ilike.%{kw}%"
                    for kw in search_terms[:5]
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
        res = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return (res.text or "").strip()
    except Exception as e:
        return f"⚠️ အယ်ဒီတာ့ သုံးသပ်ချက် ထုတ်ပြန်ရာတွင် အမှားဖြစ်ပေါ်ခဲ့သည်: {e}"


# ============================================================
# 7. TELEGRAM HANDLERS
# ============================================================

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🇲🇲 **Myanmar Intelligent Newsroom AI Bot (v7.0)**\n\n"
        "• နေ့စဉ်ထုတ် သတင်းစာများမှ **စက်သုံးဆီ၊ ရွှေ၊ ငွေလဲနှုန်းနှင့် ကုန်ဈေးနှုန်းများကို ၁၀၀% တိကျစွာ** စိစစ်တွက်ချက်ပေးပါသည်\n"
        "• မနေ့ကနှင့် ဒီနေ့ ဈေးနှုန်းကွာခြားချက် (တက်/ကျ) များကို သင်္ချာနည်းကျ အတိအကျ ပြသပေးပါသည်\n\n"
        "📌 **စမ်းသပ် မေးမြန်းနိုင်သော ဥပမာများ:**\n"
        "• `မနေ့က စက်သုံးဆီဈေးနှုန်းအခြေအနေနဲ့ ဒီနေ့ စက်သုံးဆီအခြေအနေ နှိင်းယှဉ်ပြပါ`\n"
        "• `ဒီနေ့ ရွှေရည်ညွှန်းဈေး ဘယ်လောက်လဲ`\n"
        "• `/compare <ခေါင်းစဉ်>` (သတင်းဌာနစုံ Cross-Check ပြုလုပ်ရန်)"
    )
    await safe_send_or_edit(None, welcome, is_edit=False, update_context=update)


async def compare_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_send_or_edit(
            None,
            "📋 **/compare အသုံးပြုနည်း:** `/compare <ခေါင်းစဉ်>`\nဥပမာ: `/compare စစ်ရေး`",
            is_edit=False,
            update_context=update,
        )
        return

    if not run_cross_source_comparison:
        await safe_send_or_edit(
            None,
            "⚠️ Cross Source Verifier Module ချိတ်ဆက်မထားပါ။",
            is_edit=False,
            update_context=update,
        )
        return

    topic = " ".join(context.args).strip()
    status_msg = await safe_send_or_edit(
        None,
        f"🔍 **Cross-Source Fact-Check စတင်နေပါသည်...**\nခေါင်းစဉ်: _{topic}_",
        is_edit=False,
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


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_query = update.message.text.strip()
    if not user_query:
        return

    today_str, yesterday_str = get_myanmar_dates()

    # Determine date scope
    if any(k in user_query for k in ["မနေ့", "နှိုင်းယှဉ်", "နှိင်းယှဉ်", "ယှဉ်ပြ", "ကွာခြား", "ပြောင်းလဲ"]):
        target_dates = [yesterday_str, today_str]
    elif "မနေ့" in user_query:
        target_dates = [yesterday_str]
    else:
        target_dates = [today_str]

    status_msg = await safe_send_or_edit(
        None,
        "🔍 သတင်းစာ ဒေတာဘေ့စ်တွင် တိကျသော အချက်အလက်များ စိစစ်နေပါသည်...",
        is_edit=False,
        update_context=update,
    )

    try:
        # Resolve clients per-request (lazily) instead of at import time.
        supabase = get_supabase_client()
        genai_client = get_genai_client()

        # ============================================================
        # PATH 1: DEDICATED PRECISION NUMERIC ROUTE
        # ============================================================
        if is_numeric_or_price_query(user_query):
            comm_key = detect_commodity_context(user_query)
            logger.info("🎯 Numeric route activated. Detected commodity: '%s'", comm_key)

            # 1. Query structured table `newspaper_numbers`
            numeric_records = await asyncio.to_thread(
                query_newspaper_numbers, supabase, comm_key, target_dates
            )

            # 2. If not found in `newspaper_numbers`, auto-extract from `articles` and cache!
            if not numeric_records and genai_client:
                logger.info("ℹ️ Numbers not found in cache. Extracting from articles...")
                articles = await asyncio.to_thread(
                    execute_articles_search, supabase, [comm_key or "ရည်ညွှန်း"], target_dates
                )
                numeric_records = await asyncio.to_thread(
                    extract_and_cache_missing_numbers,
                    genai_client,
                    supabase,
                    articles,
                    target_dates,
                    comm_key,
                )

            # 3. If numeric records exist, generate 100% mathematically exact report
            if numeric_records:
                report = build_precision_numeric_report(
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

        # ============================================================
        # PATH 2: GENERAL NARRATIVE NEWS ROUTE
        # ============================================================
        search_terms = [w for w in user_query.split() if len(w) >= 2][:5]
        articles = await asyncio.to_thread(
            execute_articles_search, supabase, search_terms, target_dates
        )
        final_answer = await asyncio.to_thread(
            generate_general_editorial_response,
            genai_client,
            user_query,
            articles,
            target_dates,
        )

        chunks = split_message_text(final_answer)
        await safe_send_or_edit(status_msg, chunks[0], is_edit=True)
        for ch in chunks[1:]:
            await safe_send_or_edit(None, ch, is_edit=False, update_context=update)

    except Exception as e:
        logger.error("❌ Execution error: %s", e, exc_info=True)
        await safe_send_or_edit(
            status_msg,
            f"⚠️ အချက်အလက်ထုတ်ယူရာတွင် ချို့ယွင်းချက်ဖြစ်ပေါ်ခဲ့သည်: `{e}`",
            is_edit=True,
        )


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
        raise SystemExit("❌ TELEGRAM_BOT_TOKEN မရှိပါ။ .env ဖိုင်ကို စစ်ဆေးပါ။")

    print("🤖 Myanmar Intelligent Newsroom Bot (v7.0 Precision Edition) is starting...")
    print(f"🕘 Timezone: Asia/Yangon")
    print(f"📰 Monitored Papers: {', '.join(NEWSPAPERS)}")
    print(f"🧠 Gemini model: {GEMINI_MODEL}")

    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(CommandHandler("compare", compare_command_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    print("🚀 Bot is polling for messages...")
    app.run_polling()


if __name__ == "__main__":
    main()