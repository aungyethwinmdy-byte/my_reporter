import os
import re
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

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

    # Fallback: စကားလုံး ၂ လုံးထက်ပိုသော အဓိက အပိုင်းအစများကို ယူခြင်း
    tokens = [t for t in re.split(r'[\s၊။]+', cleaned_hint) if len(t) > 1]
    return tokens[:3] if tokens else [user_query[:20]]

def search_articles_view_safely(supabase_client, search_keywords: list[str]):
    """
    Supabase 'articles' View ထဲမှ သတင်းများ Dynamic ရှာဖွေခြင်း
    (Strict Allowed Columns Only: article_id, newspaper_name, issue_date, page_no, headline, body_text)
    """
    if not search_keywords:
        return []

    fetched_articles = []
    seen_ids = set()

    # Performance ကောင်းစေရန် ထိပ်ဆုံး keyword ၃ ခုဖြင့်သာ အဓိက ရှာဖွေခြင်း
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

    # AI Context ထဲ ထည့်ရန် အသင့်တော်ဆုံး Top 8 ပုဒ်ကိုသာ ပြန်ပေးခြင်း
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