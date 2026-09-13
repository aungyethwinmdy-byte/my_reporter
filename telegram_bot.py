import os
import re
import json
import logging
from dotenv import load_dotenv

from supabase import create_client, Client
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Configuration & Environment Variables
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase Credentials မပြည့်စုံပါ။ .env ဖိုင်ကို စစ်ဆေးပါ။")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
genai_client = genai.Client(api_key=GEMINI_API_KEY)


# ==============================================================================
# ၁။ Chronological Query Planner
# ==============================================================================
def parse_query_for_timeline(genai_client, user_query: str) -> dict:
    """
    မေးခွန်းထဲမှ နေ့ရက်အလိုက် ခြေရာခံရမည့် အဓိက အကြောင်းအရာနှင့် Keywords များကို ခွဲထုတ်ခြင်း
    """
    prompt = f"""You are the Chief Intelligence Analyst for the Myanmar Newsroom Archive.
The user asked: "{user_query}"

Instructions:
1. Identify the subject/person (e.g. နိုင်ငံတော်သမ္မတ, ဝန်ကြီးချုပ်, အစိုးရ).
2. Identify the action/domain (e.g. ခရီးစဉ်, ရေဘေး, စီမံကိန်း).
3. Generate 3-5 standalone root keywords in Burmese to fetch daily coverage across issues from 2026-08-31 to present.

Return ONLY a valid JSON object:
{{
    "subject": "နိုင်ငံတော်သမ္မတ",
    "search_terms": ["သမ္မတ", "ခရီးစဉ်", "ချစ်ကြည်ရေး", "ကမ္ဘောဒီးယား", "ဗီယက်နမ်", "ပြည်ပခရီး"]
}}"""

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            ),
        )
        data = json.loads(response.text)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.error(f"Timeline planning error: {e}")

    return {
        "subject": "သမ္မတ",
        "search_terms": ["သမ္မတ", "ခရီးစဉ်", "ချစ်ကြည်ရေး", "ကမ္ဘောဒီးယား", "ဗီယက်နမ်"]
    }


# ==============================================================================
# ၂။ Day-by-Day Chronological Scan (ဩဂုတ် ၃၁ မှ ယနေ့အထိ ရှာဖွေခြင်း)
# ==============================================================================
def scan_daily_archive(supabase_client, search_terms: list[str]):
    """
    သတင်းစာများကို ဩဂုတ် ၃၁ မှစ၍ ရှေ့မှနောက်သို့ (issue_date ASC)
    တစ်ရက်ချင်းစီ အစီအစဉ်တကျ စစ်ဆေးဆွဲထုတ်ခြင်း
    """
    fetched_articles = []
    seen_ids = set()

    for kw in search_terms[:5]:
        clean_kw = re.sub(r'[,;\'"()%]', '', kw).strip()
        if not clean_kw or len(clean_kw) < 2:
            continue

        try:
            # issue_date ASC ဖြင့် ရှေ့မှနောက်သို့ အစီအစဉ်တကျ ရယူခြင်း
            response = supabase_client.from_("articles").select(
                "article_id, newspaper_name, issue_date, page_no, headline, body_text"
            ).gte(
                "issue_date", "2026-08-31"
            ).or_(
                f"headline.ilike.%{clean_kw}%,body_text.ilike.%{clean_kw}%"
            ).order("issue_date", desc=False).limit(20).execute()

            for row in (response.data or []):
                aid = row.get("article_id")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    fetched_articles.append(row)
        except Exception as e:
            logger.error(f"Archive scan error for keyword '{clean_kw}': {e}")

    # ရက်စွဲအလိုက် အစဉ်အတိုင်း စီတန်းခြင်း
    fetched_articles.sort(key=lambda x: str(x.get("issue_date", "")))
    return fetched_articles


# ==============================================================================
# ၃။ Chronological Fact Synthesis (နေ့ရက်အလိုက် စိစစ်ထုတ်ပြန်ခြင်း)
# ==============================================================================
def synthesize_chronological_report(genai_client, user_query: str, retrieved_articles: list) -> str:
    if not retrieved_articles:
        return "⚠️ **ဒေတာဘေ့စ်တွင် မတွေ့ရှိပါ**\n\nဩဂုတ် ၃၁ ရက်နေ့မှစ၍ ယနေ့အထိ သတင်းစာမှတ်တမ်းများတွင် မေးမြန်းထားသော အချက်အလက် မတွေ့ရှိပါ။"

    # သတင်းများကို နေ့ရက်အလိုက် အစုဖွဲ့ခြင်း
    timeline_context = ""
    current_date = ""

    for art in retrieved_articles:
        date_str = art.get("issue_date")
        if date_str != current_date:
            current_date = date_str
            timeline_context += f"\n\n==================== [📅 ရက်စွဲ: {current_date}] ====================\n"

        timeline_context += (
            f"• သတင်းစာ: {art.get('newspaper_name')} (စာမျက်နှာ {art.get('page_no')})\n"
            f"  ခေါင်းစဉ်: {art.get('headline')}\n"
            f"  စာသားအနှစ်ချုပ်: {art.get('body_text', '')[:1200]}\n"
        )

    system_prompt = f"""You are the Executive Editor of the Myanmar Intelligent Newsroom.
User Question: "{user_query}"

Below are daily newspaper coverage entries retrieved from the database, strictly arranged from 2026-08-31 onwards:
{timeline_context}

EDITORIAL REQUIREMENTS:
1. Conduct a date-by-date audit (e.g. Check Aug 31, Sept 1, Sept 2, up to the latest date).
2. Answer the user's specific question directly with clear dates and country names.
3. If asking about foreign visits prior to Cambodia, list all visited countries chronologically with dates.
4. Output strictly in the following clean Myanmar layout:

🇲🇲 **နေ့ရက်အလိုက် စိစစ်တွေ့ရှိချက် အစီရင်ခံစာ**
──────────────────────────
[မေးခွန်းအတွက် တိုက်ရိုက်ရှင်းလင်းသော အနှစ်ချုပ် အဖြေ ၁-၂ ကြောင်း]

📍 **ရက်စွဲအလိုက် ဖြစ်စဉ်မှတ်တမ်း (Timeline):**

• **[YYYY-MM-DD] :** [ဖြစ်စဉ် / သွားရောက်ခဲ့သည့် နိုင်ငံနှင့် အကြောင်းအရာ]  
  📰 [သတင်းစာအမည်] (စာမျက်နှာ [X])

• **[YYYY-MM-DD] :** [ဖြစ်စဉ် / သွားရောက်ခဲ့သည့် နိုင်ငံနှင့် အကြောင်းအရာ]  
  📰 [သတင်းစာအမည်] (စာမျက်နှာ [X])

──────────────────────────
📌 **နိဂုံးချုပ် သုံးသပ်ချက်:**
[မေးခွန်းနှင့် ပတ်သက်၍ အတိအကျ စိစစ်တွေ့ရှိရသော အချက်အလက် အနှစ်ချုပ်]"""

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=system_prompt,
        )
        return response.text
    except Exception as e:
        logger.error(f"Synthesis error: {e}")
        return "⚠️ **အစီရင်ခံစာ ထုတ်ပြန်ရာတွင် ချို့ယွင်းချက် ဖြစ်ပေါ်ခဲ့ပါသည်။**"


# ==============================================================================
# ၄။ Telegram Handlers
# ==============================================================================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🇲🇲 **Myanmar Intelligent Newsroom AI Bot မှ ကြိုဆိုပါသည်**\n\n"
        "ဒေတာဘေ့စ်ထဲရှိ သတင်းစာများကို ဩဂုတ် ၃၁ မှစ၍ နေ့ရက်အလိုက် အသေးစိတ် စစ်ဆေးပေးနိုင်ပါသည်။"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text.strip()
    if not user_query:
        return

    logger.info(f"Received query: '{user_query}'")
    status_msg = await update.message.reply_text("🔍 ဩဂုတ် ၃၁ မှစ၍ နေ့ရက်အလိုက် သတင်းစာများကို စိစစ်နေပါသည်...")

    try:
        # Step 1: Planning
        plan = parse_query_for_timeline(genai_client, user_query)
        search_terms = plan.get("search_terms", [user_query])

        # Step 2: Day-by-day Retrieval
        articles = scan_daily_archive(supabase, search_terms)
        logger.info(f"Retrieved {len(articles)} chronological articles.")

        # Step 3: Synthesis
        final_answer = synthesize_chronological_report(genai_client, user_query, articles)

        if len(final_answer) > 4000:
            final_answer = final_answer[:3990] + "\n\n..."

        try:
            await status_msg.edit_text(final_answer, parse_mode="Markdown")
        except Exception:
            await status_msg.edit_text(final_answer)

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await status_msg.edit_text("⚠️ စနစ်အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။ ပြန်လည်မေးမြန်းပေးပါ။")


def main():
    print("🤖 Myanmar Intelligent Newsroom AI Bot is starting...")
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.run_polling()


if __name__ == "__main__":
    main()