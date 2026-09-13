import os
import re
import json
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client, Client
from google import genai
from google.genai import types

# .env ဖိုင်မှ အချက်အလက်များ လုတ်ယူခြင်း
load_dotenv()

# Logging သတ်မှတ်ခြင်း
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Credentials ရယူခြင်း
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8521816249:AAGYUsPI7mg1iWoboEo1_fBKJZ1e9rWdymM")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://zdsxuxwonkovuesjepfa.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Supabase Client ဖန်တီးခြင်း
supabase_client: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Gemini Client ဖန်တီးခြင်း (Official google-genai SDK)
genai_client = None
if GEMINI_API_KEY:
    genai_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    genai_client = genai.Client()

# ၁။ မြန်မာ စကားပြော Filler များကို အရှည်ဆုံးမှ အတိုသို့ သတ်မှတ်ခြင်း
BURMESE_FILLERS = sorted([
    "အကြောင်းကို သိချင်ပါတယ်", "အကြောင်း သိချင်ပါတယ်", "အကြောင်း ပြောပြပါ",
    "စုံစမ်းချင်လို့ပါ", "သိချင်လို့ပါ", "သိလိုပါတယ်", "ပြောပြပေးပါ",
    "ရှိတယ်လေ", "ရှိပါတယ်", "အကြောင်းလေး", "အကြောင်းအရာ", "အကြောင်း",
    "ပတ်သက်ပြီး", "ပတ်သက်၍", "ရှိတယ်", "အခု", "နော်", "ပါ", "လဲ", "လည်း",
    "ဘာလဲ", "ဘယ်သူလဲ", "ဘယ်လဲ", "ဘယ်အချိန်လဲ", "ဘယ်မှာလဲ", "ဆိုတာ"
], key=len, reverse=True)

def strip_burmese_fillers(text: str) -> str:
    """စကားပြော အဆာသွပ် စာသားများကို သန့်စင်ပေးခြင်း"""
    cleaned = text
    for filler in BURMESE_FILLERS:
        cleaned = cleaned.replace(filler, " ")
    return " ".join(cleaned.split()).strip()

def dynamic_ai_query_parser(genai_client, user_query: str) -> list[str]:
    """
    Gemini AI မှ မေးခွန်း၏ ပင်မ Intent / Temporal Sequence ကို သုံးသပ်ပြီး
    Database ထဲ ရှာရန် သင့်တော်သော Search Terms များ ထုတ်ပေးခြင်း
    """
    cleaned_hint = strip_burmese_fillers(user_query)
    
    prompt = f"""You are an advanced Myanmar Intelligent Newsroom AI Query Parser.
Deconstruct the following Myanmar natural language query from a news editor.

User Query: "{user_query}"
Cleaned Hint: "{cleaned_hint}"

Instructions:
1. Understand the core intent, implicitly referenced events, entities, timeline sequences, or relative locations.
2. Extract and generate 2 to 4 high-precision search keywords in Burmese that would best fetch relevant newspaper articles from the database.
3. If the query asks for relative events (e.g. 'event before X', 'visited country before Y'), generate keywords for X, the target action (e.g. ခရီးစဉ်), and related entities.
4. Keep each keyword concise (1-3 words max, NO punctuation or commas).

Return ONLY a valid JSON array of strings.
Example: ["ကမ္ဘောဒီးယား", "ခရီးစဉ်"]"""

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        data = json.loads(response.text)
        if isinstance(data, list) and data:
            return [str(k).strip() for k in data if k]
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v:
                    return [str(k).strip() for k in v if k]
    except Exception as e:
        logger.error(f"Dynamic query parser error: {e}")

    tokens = [t for t in re.split(r'[\s၊။]+', cleaned_hint) if len(t) > 1]
    return tokens[:3] if tokens else [user_query[:20]]

def search_articles_view_safely(supabase_client, search_keywords: list[str]):
    """
    Supabase 'articles' View ထဲမှ သတင်းများ Dynamic ရှာဖွေခြင်း
    (Strict Allowed Columns Only: article_id, newspaper_name, issue_date, page_no, headline, body_text)
    """
    if not supabase_client or not search_keywords:
        return []

    fetched_articles = []
    seen_ids = set()

    for kw in search_keywords[:3]:
        clean_kw = re.sub(r'[,;\'"()%]', '', kw).strip()
        if not clean_kw or len(clean_kw) < 2:
            continue

        try:
            response = supabase_client.from_("articles").select(
                "article_id, newspaper_name, issue_date, page_no, headline, body_text"
            ).or_(
                f"headline.ilike.%{clean_kw}%,body_text.ilike.%{clean_kw}%"
            ).order("issue_date", descending=True).limit(6).execute()

            for row in (response.data or []):
                aid = row.get("article_id")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    fetched_articles.append(row)
        except Exception as e:
            logger.error(f"Error querying 'articles' view for keyword '{clean_kw}': {e}")

    return fetched_articles[:8]

def generate_newsroom_ai_response(genai_client, user_query: str, retrieved_articles: list) -> str:
    """
    ရှာဖွေရရှိသော သတင်းများမှ မေးခွန်း၏ အဖြေကို AI က သုံးသပ်ထုတ်နှုတ်ပြီး Citation ဖြင့် ဖြေကြားခြင်း
    """
    if not retrieved_articles:
        return "⚠️ **အချက်အလက် မတွေ့ရှိပါ**\n\nတောင်းပန်ပါတယ်။ မေးမြန်းထားသော အကြောင်းအရာနှင့် ပတ်သက်သည့် သတင်းမှတ်တမ်း ဒေတာဘေ့စ်တွင် မတွေ့ရှိပါ။"

    articles_context = ""
    for idx, art in enumerate(retrieved_articles, 1):
        articles_context += f"\n--- Article [{idx}] ---\n"
        articles_context += f"Newspaper: {art.get('newspaper_name')}\n"
        articles_context += f"Date: {art.get('issue_date')}\n"
        articles_context += f"Page: {art.get('page_no')}\n"
        articles_context += f"Headline: {art.get('headline')}\n"
        articles_context += f"Text Snippet: {art.get('body_text', '')[:1000]}\n"

    system_prompt = f"""You are the Executive Newsroom AI Editor.
The user asked a question: "{user_query}"

Candidate Newspaper Articles retrieved from Supabase 'articles' view:
{articles_context}

Your Task:
1. Analyze the context from the articles to directly and accurately answer the user's question.
2. Perform necessary chronological and logical synthesis (e.g., identify sequences of visits, events before/after, key actions).
3. Provide a professional, concise, executive-level response in Myanmar language.
4. NEVER invent facts or citations not present in the provided articles.
5. For EVERY factual claim, strictly cite the source using ONLY this format:
   [Source: Newspaper Name | YYYY-MM-DD | Page X]"""

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=system_prompt
        )
        return response.text
    except Exception as e:
        logger.error(f"Response generation error: {e}")
        return "⚠️ **အဖြေ ထုတ်ပြန်ရာတွင် အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။**"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = (
        "🏛️ **Myanmar Intelligent Newsroom AI Chatbot** သို့ ကြိုဆိုပါသည်။\n\n"
        "မြန်မာ့အလင်း နှင့် ကြေးမုံ သတင်းစာများမှ သတင်းအချက်အလက်များကို AI စနစ်ဖြင့် ရှာဖွေ မေးမြန်းနိုင်ပါသည်။"
    )
    await update.message.reply_text(welcome_msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_query = update.message.text
    if not user_query:
        return

    logger.info(f"Received query from Telegram: '{user_query}'")
    status_msg = await update.message.reply_text("🔍 သတင်းဒေတာဘေ့စ်တွင် ရှာဖွေဆန်းစစ်နေပါသည်...")

    try:
        keywords = dynamic_ai_query_parser(genai_client, user_query)
        logger.info(f"Extracted keywords: {keywords}")

        articles = search_articles_view_safely(supabase_client, keywords)
        logger.info(f"Fetched {len(articles)} articles from database.")

        answer = generate_newsroom_ai_response(genai_client, user_query, articles)
        await status_msg.edit_text(answer)
    except Exception as e:
        logger.error(f"Error handling user message: {e}")
        await status_msg.edit_text("⚠️ **မေးမြန်းမှုအား ဆန်းစစ်ရာတွင် အမှားအယွင်း ဖြစ်ပေါ်ခဲ့ပါသည်။**")

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Error: TELEGRAM_BOT_TOKEN is missing!")
        return

    print("🤖 Myanmar Intelligent Newsroom AI Bot is starting...")
    print("Press Ctrl+C to stop.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()