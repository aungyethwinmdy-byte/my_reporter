import os
import re
import json
import telebot
from datetime import datetime
import pytz
from html import escape
from dotenv import load_dotenv
from supabase import create_client, Client
from google import genai
from google.genai import types
from concurrent.futures import ThreadPoolExecutor

# Load environment variables from .env file for local development
load_dotenv()

# Credentials & Configurations from Environment Variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase environment variables are not set!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Gemini API Client
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = None 
if GEMINI_API_KEY: 
    try: 
        gemini_client = genai.Client(api_key=GEMINI_API_KEY) 
    except Exception as e: 
        print(f"Gemini Init Error: {e}") 

GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.6-flash", "gemini-flash-latest"] 
executor = ThreadPoolExecutor(max_workers=10) 
MMT = pytz.timezone('Asia/Yangon') 
VALID_INTENTS = {"DAILY_BRIEFING", "EVENT_TIMELINE", "COMPARE", "SEARCH"} 

# Intent Display Metadata
INTENT_META = {
    "DAILY_BRIEFING": ("🗞 DAILY BRIEFING", "နေ့စဉ်သတင်း အနှစ်ချုပ် သုံးသပ်ချက်"),
    "EVENT_TIMELINE": ("⏳ EVENT TIMELINE", "သတင်းစဉ်ဆက် သမိုင်းကြောင်း ပြက္ခဒိန်"),
    "COMPARE": ("⚖️ CROSS-SOURCE COMPARISON", "သတင်းစာများ အချက်အလက် နှိုင်းယှဉ်ချက်"),
    "SEARCH": ("🔍 FACT SHEET SEARCH", "သတင်းစာပါ အချက်အလက် ရှာဖွေမှု")
}

def call_gemini_api(contents, config=None): 
    if not gemini_client: 
        return None 
    # Disable AFC explicitly to prevent SDK warning logs
    if config is None:
        config = types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
        )
    else:
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)

    for model in GEMINI_MODELS: 
        try: 
            return gemini_client.models.generate_content(model=model, contents=contents, config=config) 
        except Exception as e: 
            print(f"⚠️ Gemini error [{model}]: {e}") 
    return None 

def extract_fallback_keyword(user_text): 
    """
    Robust Unsegmented Burmese Keyword Extractor.
    Safely handles joined words (e.g. 'ရေဘေးသတင်းဖော်ပြပေးပါ' -> 'ရေဘေး').
    Protects proper nouns like 'ရုရှား' by banning substring replacement on short ambiguous particles.
    """
    if not user_text or not isinstance(user_text, str):
        return ""

    # Multi-word conversational fillers across all news sectors (sorted longest first)
    fillers = [
        "သတင်းစာတွေ", "သတင်းစာများ", "သတင်းစာပါ", "သတင်းစာ", "သတင်းတွေ", "သတင်းများ", "သတင်း",
        "ဖော်ပြပေးပါ", "ဖော်ပြပေး", "ပြောပြပါ", "ရှာပေးပါ", "ပြပေးပါ", "တင်ပေးပါ", "မေးချင်တယ်",
        "ဘာပါလဲ", "ဘာလဲ", "ပါလဲ", "ပါသလဲ", "အကြောင်းတွေ", "အကြောင်းများ", "အကြောင်း", 
        "အခြေအနေများ", "အခြေအနေ", "အပြောင်းအလဲများ", "အပြောင်းအလဲ", "ထုတ်ပြန်ချက်များ", "ထုတ်ပြန်ချက်",
        "ဖြစ်စဉ်များ", "ဖြစ်စဉ်", "ကိစ္စများ", "ကိစ္စ", "အချက်အလက်များ", "အချက်အလက်",
        "သိချင်ပါတယ်", "လိုချင်တယ်", "ကြည့်ချင်တယ်", "ဖတ်ချင်တယ်", "နှိုင်းယှဉ်ပြပါ", "နှိုင်းယှဉ်", 
        "နောက်ဆုံးလုပ်ခဲ့တဲ့", "ကျင်းပခဲ့တဲ့", "ပြောကြားခဲ့တဲ့", "တွေ့ဆုံခဲ့တဲ့",
        "timeline", "Timeline", "TIMELINE", "compare", "COMPARE", "briefing", "BRIEFING",
        "နဲ့ပတ်သက်ပြီး", "နဲ့ပတ်သက်တဲ့", "ပတ်သက်ပြီး", "ပတ်သက်တဲ့", "အလိုက်", "အတွက်",
        "ရမလဲ", "ဘယ်လို", "ဖော်ပြ", "ပြပါ"
    ]
    fillers.sort(key=len, reverse=True)
    
    cleaned = user_text
    for filler in fillers:
        cleaned = re.sub(re.escape(filler), " ", cleaned, flags=re.IGNORECASE)

    # Clean non-Myanmar/non-alphanumeric punctuation except whitespace
    cleaned = re.sub(r'[^\u1000-\u109F0-9a-zA-Z\s]', ' ', cleaned)
    
    tokens = [t.strip() for t in cleaned.split() if t.strip()]

    # Safely strip sentence-ending particles ONLY if attached to token end and length is preserved
    cleaned_tokens = []
    particles = ["နဲ့", "တာ", "ရော", "တဲ့", "မှာ", "က", "ကို", "ရဲ့", "၏", "မှ", "ဖြင့်"]
    for token in tokens:
        term = token
        for p in particles:
            if term.endswith(p) and len(term) > len(p) + 2:
                term = term[:-len(p)].strip()
                break
        if term:
            cleaned_tokens.append(term)

    if not cleaned_tokens:
        return user_text.strip()

    if len(cleaned_tokens) > 3:
        return " ".join(cleaned_tokens[:3])
    return " ".join(cleaned_tokens)

def analyze_user_query(user_text): 
    lower_text = user_text.lower()
    detected_intent = "SEARCH"
    if "timeline" in lower_text or "သမိုင်း" in lower_text or "စဉ်ဆက်" in lower_text:
        detected_intent = "EVENT_TIMELINE"
    elif "နှိုင်းယှဉ်" in lower_text or "compare" in lower_text or "ဘာကွာလဲ" in lower_text:
        detected_intent = "COMPARE"
    elif "အကျဉ်းချုပ်" in lower_text or "briefing" in lower_text:
        detected_intent = "DAILY_BRIEFING"

    extracted_kw = extract_fallback_keyword(user_text)
    complex_triggers = ["timeline", "နှိုင်းယှဉ်", "အကျဉ်းချုပ်", "briefing", "compare", "ကွာခြားချက်", "ဘာကွာလဲ", "သမိုင်း", "စဉ်ဆက်"] 
    is_complex = any(trigger in lower_text for trigger in complex_triggers) or len(user_text.split()) > 4 

    if not is_complex: 
        return {"intent": detected_intent, "keyword": extracted_kw, "date": None} 

    prompt = f'''Analyze the user's query for a Myanmar Newsroom AI.
Query: "{user_text}"

INSTRUCTIONS:
1. Identify intent: "DAILY_BRIEFING", "EVENT_TIMELINE", "COMPARE", or "SEARCH".
2. Extract the CORE subject/entity keyword. Strip ALL conversational fillers (e.g. "သတင်း", "ဖော်ပြပေးပါ", "အကြောင်း", "အခြေအနေ", "သိချင်ပါတယ်", "နဲ့ပတ်သက်ပြီး").
Examples:
- "ရေဘေးသတင်းဖော်ပြပေးပါ" -> keyword: "ရေဘေး"
- "ငလျင်အပြောင်းအလဲရော" -> keyword: "ငလျင်"
- "ငါးမွေးမြူရေးအခြေအနေ" -> keyword: "ငါးမွေးမြူရေး"
- "စိုက်ပျိုးရေးနဲ့ စက်သုံးဆီ compare လုပ်ပေးပါ" -> keyword: "စိုက်ပျိုးရေး စက်သုံးဆီ"

3. Extract date if present in YYYY-MM-DD format, else null.

Return strictly JSON:
{{"intent": "CATEGORY", "keyword": "core_entity", "date": "YYYY-MM-DD or null"}}'''

    config = types.GenerateContentConfig(response_mime_type="application/json") 
    res = call_gemini_api(prompt, config=config) 
    data = {} 
    if res and res.text: 
        try: 
            cleaned_json = re.sub(r'^```json\s*|\s*```$', '', res.text.strip(), flags=re.MULTILINE) 
            data = json.loads(cleaned_json) 
            raw_kw = data.get("keyword")
            if not raw_kw or not isinstance(raw_kw, str):
                raw_kw = user_text
            data["keyword"] = extract_fallback_keyword(raw_kw)
        except Exception as e: 
            print(f"JSON parse error: {e}") 

    if data.get("intent") not in VALID_INTENTS: 
        data["intent"] = detected_intent 
    if not isinstance(data.get("keyword"), str) or not data.get("keyword").strip(): 
        data["keyword"] = extracted_kw 
    if data.get("date"): 
        try: 
            datetime.strptime(data["date"], "%Y-%m-%d") 
        except ValueError: 
            data["date"] = None 
    return data 

def generate_answer(user_query, db_records, keyword, intent): 
    if not db_records: 
        return f"> ⚠️ **အချက်အလက် မတွေ့ရှိပါ**\n\nတောင်းပန်ပါတယ်။ '{keyword}' နှင့် ပတ်သက်သော သတင်းမှတ်တမ်း ဒေတာဘေ့စ်တွင် မတွေ့ရှိပါ။" 
    
    context_str = "" 
    for idx, row in enumerate(db_records): 
        body_snippet = str(row.get('body_text', ''))[:800] 
        newspaper = row.get('newspaper_name', 'သတင်းစာ') 
        date = row.get('issue_date', '-') 
        page = row.get('page_no', '-') 
        headline = row.get('headline', '-') 
        context_str += f"[{idx+1}] Source: {newspaper}, {date}, Page {page}\nHeadline: {headline}\nContent: {body_snippet}...\n\n" 
    
    sys_inst = f"""
    You are Myanmar Intelligent Newsroom AI.
    Your task is to generate a professional, highly-structured executive briefing for Telegram readers in Burmese.

    GENERAL RULES:
    1. Use ONLY supplied context. NEVER invent facts, dates, names, or sources.
    2. Start the response with a short Executive Summary blockquote using '> 📌 **အကျဉ်းချုပ်နိဒါန်း**\n> Summary text here...'.
    3. Mandatory Citation Format: `[Source: Newspaper Name | YYYY-MM-DD | Page X]`.
    4. Answer in formal Burmese language with bold headings and key figures.

    INTENT-SPECIFIC FORMATTING GUIDELINES (Current Intent: {intent}):

    If intent is 'EVENT_TIMELINE':
    Use a clean tree timeline format:
    🗓 **YYYY-MM-DD**
    ├ 🔹 **[Headline/Event Title]**
    ├ [Brief event description with bold key figures]
    └ 📰 `[Source: Newspaper | YYYY-MM-DD | Page X]`

    If intent is 'COMPARE':
    Use a cross-source comparison structure:
    📊 **[Topic/Category Name]**
    ├ 📰 **မြန်မာ့အလင်း:** [Reported facts] `[Source: ...]`
    ├ 📰 **ကြေးမုံ:** [Reported facts] `[Source: ...]`
    └ 💡 **ကွာခြားချက် အနှစ်ချုပ်:** [Key differences or note]

    If intent is 'DAILY_BRIEFING':
    Group by Sector/Topic:
    📌 **[Sector Name, e.g., နိုင်ငံရေး/စီးပွားရေး]**
    ├ 🔹 **[Topic Headline]**
    ├ [Summary details]
    └ 📰 `[Source: ...]`

    If intent is 'SEARCH':
    Use clear bullet-point fact sheets:
    📌 **[Key Finding/Headline]**
    ├ [Detailed factual narrative]
    └ 📰 `[Source: ...]`
    """ 
    
    config = types.GenerateContentConfig(system_instruction=sys_inst, temperature=0.1)
    res = call_gemini_api(f"User Query: {user_query}\n\nContext:\n{context_str}", config=config) 
    if res and res.text: 
        return res.text 
    return f"📑 တွေ့ရှိသော သတင်းမှတ်တမ်းများ ({len(db_records)} ခု):\n\n" + context_str 

def process_message_task(message): 
    chat_id = message.chat.id 
    user_text = message.text 
    msg = bot.reply_to(message, "⏳ <b>သတင်းစာ Database မှ ရှာဖွေစိစစ်နေပါသည်...</b>", parse_mode="HTML") 
    analysis = analyze_user_query(user_text) 
    intent = analysis.get("intent", "SEARCH") 
    keyword = analysis.get("keyword", "").strip() 
    db_records = [] 
    
    # Tier 1: RPC Search
    try: 
        if intent == "DAILY_BRIEFING": 
            query_date = analysis.get("date") or datetime.now(MMT).strftime('%Y-%m-%d') 
            res = supabase.rpc("fn_get_daily_briefing", {"p_date": query_date}).execute() 
        elif intent == "EVENT_TIMELINE": 
            res = supabase.rpc("fn_get_event_timeline", {"p_keyword": keyword}).execute() 
        elif intent == "COMPARE": 
            res = supabase.rpc("fn_compare_sources", {"p_keyword": keyword}).execute() 
        else: 
            res = supabase.rpc("fn_search_news", {"p_query": keyword, "p_limit": 10}).execute() 
        if res.data: 
            db_records = res.data 
    except Exception as e: 
        print(f"RPC Error [{intent}]: {e}") 

    # Tier 2 & 3: Multi-Term Fallback Search
    if not db_records and keyword: 
        tokens = [t.strip() for t in keyword.split() if len(t.strip()) > 1]
        search_terms = [keyword] + tokens
        
        for term in search_terms:
            clean_term = term.replace(",", " ").strip()
            if not clean_term: continue
            try:
                res = (supabase.table("articles")
                       .select("article_id,newspaper_name,issue_date,page_no,headline,body_text")
                       .or_(f"body_text.ilike.%{clean_term}%,headline.ilike.%{clean_term}%")
                       .order("issue_date", desc=True)
                       .limit(10)
                       .execute())
                if res.data:
                    db_records = res.data
                    break
            except Exception as fb_e:
                print(f"Table Fallback Error [{clean_term}]: {fb_e}")

    final_answer = generate_answer(user_query=user_text, db_records=db_records, keyword=keyword, intent=intent) 
    try: bot.delete_message(chat_id=chat_id, message_id=msg.message_id) 
    except Exception: pass 
    
    safe_keyword = escape(keyword) 
    safe_intent = escape(intent) 
    
    icon_title, desc = INTENT_META.get(intent, ("📰 INTELLIGENT NEWSROOM", "သတင်းစာ အချက်အလက်"))
    
    # Premium Newspaper Dashboard Header
    header = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{icon_title}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <code>ENTITY   : {safe_keyword}</code>\n"
        f"📂 <code>CATEGORY : {safe_intent}</code>\n"
        f"📅 <code>UPDATED  : {datetime.now(MMT).strftime('%Y-%m-%d')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    
    try: 
        bot.send_message(chat_id, header, reply_to_message_id=message.message_id, parse_mode="HTML") 
    except Exception as e: 
        print(f"Header error: {e}") 
        
    for i in range(0, len(final_answer), 4000): 
        try: 
            bot.send_message(chat_id, final_answer[i:i+4000]) 
        except Exception as e: 
            print(f"Body error: {e}") 

@bot.message_handler(commands=['start', 'help']) 
def send_welcome(message): 
    welcome_msg = (
        "👋 <b>Intelligent Newsroom Assistant မှ ကြိုဆိုပါတယ်!</b>\n\n"
        "မြန်မာ့အလင်း နှင့် ကြေးမုံ သတင်းစာပါ အကြောင်းအရာများကို အောက်ပါအတိုင်း စနစ်တကျ မေးမြန်းနိုင်ပါသည် -\n\n"
        "🗞 <b>Daily Briefing:</b> <code>၂၀၂၆ စက်တင်ဘာ ၄ ရက်နေ့ သတင်းစာ အကျဉ်းချုပ်</code>\n"
        "⏳ <b>Timeline:</b> <code>ထားဝယ်ရေနက်ဆိပ်ကမ်း timeline ပြပါ</code>\n"
        "⚖️ <b>Compare:</b> <code>စက်သုံးဆီ ဈေးနှုန်း သတင်းစာ နှစ်စောင် နှိုင်းယှဉ်ပြပါ</code>\n"
        "🔍 <b>Search:</b> <code>ရွှေတိဂုံစေတီတော် ရွှေသင်္ကန်း လှူဒါန်းမှု သတင်း</code>"
    )
    bot.reply_to(message, welcome_msg, parse_mode="HTML") 

@bot.message_handler(func=lambda message: True) 
def handle_all_messages(message): 
    executor.submit(process_message_task, message) 

if __name__ == "__main__": 
    print("🚀 Telegram Bot is running with Environment Variables & Executive UI Design...") 
    bot.infinity_polling(timeout=20, long_polling_timeout=10)