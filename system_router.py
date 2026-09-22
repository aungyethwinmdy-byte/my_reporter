"""
================================================================
SYSTEM ROUTER & GENERAL QUERY HANDLER
Handles:
1. Greeting & small-talk filter ("မင်္ဂလာပါ", "ဟယ်လို")
2. Custom commands: /sources and /status
3. Natural language meta-queries ("သတင်းဌာန ဘယ်နှစ်ခုလဲ")
4. General AI conversation without "သတင်းစာထဲ မပါပါ" errors
================================================================
"""

import logging
from typing import Optional
from supabase import Client
from google import genai
from gemini_config import generate_content_with_fallback, resolve_models

logger = logging.getLogger("SystemRouter")

# စနစ်အတွင်း စောင့်ကြည့်ချိတ်ဆက်ထားသော မီဒီယာရင်းမြစ်များ စာရင်း
MONITORED_SOURCES = {
    "newspapers": [
        "မြန်မာ့အလင်း သတင်းစာ",
        "ကြေးမုံ သတင်းစာ",
    ],
    "independent_media": [
        "BBC Burmese (ဘီဘီစီ)",
        "The Irrawaddy (ဧရာဝတီ)",
        "RFA Burmese (လွတ်လပ်တဲ့ အာရှအသံ)",
        "PPIB (သမ္မတရုံး သတင်းလွှာ)",
    ],
}


# ============================================================
# 1. GREETING & CASUAL FILTER
# ============================================================

_GREETINGS = (
    "မင်္ဂလာပါ", "မင်္ဂလာ", "ဟယ်လို", "hello", "hi", "hey",
    "နေကောင်းလား", "နေကောင်းရဲ့လား", "ကျေးဇူး", "ကျေးဇူးတင်ပါတယ်",
    "ကျေးဇူးပါ", "thanks", "thank you", "good morning", "good evening",
)

# Only these may follow a greeting for the message to still count as a greeting.
_POLITE_SUFFIXES = (
    "ခင်ဗျာ", "ခင်ဗျ", "ရှင့်", "ရှင်", "ဗျ", "ပါ", "ရဲ့",
    "there", "all", "everyone", "again",
)

def _strip_politeness(text: str) -> str:
    """Drop trailing politeness words/punctuation so a greeting can be compared.

    "မင်္ဂလာပါခင်ဗျာ" is the same greeting as "မင်္ဂလာပါ", and "hi there" as
    "hi", so peel the suffixes off before deciding what is left.
    """
    text = text.strip()
    changed = True
    while changed:
        changed = False
        for suffix in _POLITE_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text.strip("!?.,;:~ \t\n\u200b")


def is_greeting_or_casual(query: str) -> bool:
    """မင်္ဂလာပါ၊ ဟယ်လို စသည့် နှုတ်ဆက်စကား သက်သက် ဟုတ်/မဟုတ် စစ်ဆေးခြင်း

    The match must consume the whole message, not just its opening word. The
    old rule was ``startswith(greeting) and len(query) <= 15``, which swallowed
    real questions that merely opened with a greeting — "hi ရွှေဈေး" (hi + gold
    price) was answered with a hello and the price question was dropped. A
    greeting followed by anything but politeness is not a greeting.
    """
    if not query:
        return False
    clean_q = _strip_politeness(query.lower())
    if not clean_q:
        return False

    for greeting in _GREETINGS:
        if clean_q == greeting:
            return True
        if clean_q.startswith(greeting) and not _strip_politeness(clean_q[len(greeting):]):
            return True
    return False


def get_greeting_response() -> str:
    """ယဉ်ကျေးသော နှုတ်ဆက်စကားနှင့် လမ်းညွှန်ချက် အတိုချုပ်"""
    return (
        "မင်္ဂလာပါခင်ဗျာ! 🙏\n"
        "ကျွန်တော်က **Myanmar Intelligent Newsroom AI Bot** ဖြစ်ပါတယ်။\n\n"
        "📌 **ကျွန်တော့်ကို အောက်ပါအတိုင်း မေးမြန်းနိုင်ပါတယ်ခင်ဗျာ:**\n\n"
        "⛽ **စက်သုံးဆီ / ရွှေဈေးနှုန်းများ:**\n"
        "   • `မနေ့က စက်သုံးဆီဈေးနဲ့ ဒီနေ့ဈေး နှိုင်းယှဉ်ပြပါ`\n"
        "   • `ဒီနေ့ ရွှေရည်ညွှန်းဈေး ဘယ်လောက်လဲ`\n\n"
        "📰 **သတင်းစာပါ အကြောင်းအရာများ:**\n"
        "   • `တောင်ငူ ရေကြီးမှု အခြေအနေ ဘယ်လိုရှိလဲ`\n"
        "   • `ကမ္ဘောဒီးယား ခရီးစဉ်အကြောင်း သတင်း`\n\n"
        "🌐 **သတင်းဌာနစုံ စိစစ်ရန်:**\n"
        "   • `/compare <ခေါင်းစဉ်>`\n\n"
        "⚙️ **စနစ်အချက်အလက်:** `/sources` သို့မဟုတ် `/status`"
    )


# ============================================================
# 2. SYSTEM META & SOURCES REPORT
# ============================================================

# Unambiguous references to THIS system — safe to match on their own.
_STRONG_META_MARKERS = (
    "စနစ်အခြေအနေ", "bot အကြောင်း", "ဘာတွေထည့်ထား", "ဘယ်သတင်းတွေပါ",
    "status", "source", "sources",
)

# Ordinary news vocabulary. "မီဒီယာ" (media) and "သတင်းဌာန" (news agency) turn up
# in real news questions, so on their own they must NOT mean "tell me about the
# system" — they only count when paired with a counting/enumeration form.
_TOPIC_WORDS = ("သတင်းဌာန", "မီဒီယာ")

_COUNTING_FORMS = ("ဘယ်နှ", "ဘယ်မီဒီယာ", "စာရင်း")


def is_system_meta_query(query: str) -> bool:
    """စနစ်အကြောင်း၊ သတင်းဌာန အရေအတွက်အကြောင်း မေးမြန်းခြင်း ဟုတ်/မဟုတ် စစ်ဆေးခြင်း

    This route runs before the article search, so a false positive here means
    the user's question is replaced by the canned source list. Matching the bare
    topical words used to do exactly that:

        "မီဒီယာတွေအပေါ် ဖိအားပေးမှုသတင်း"  (news about pressure on the media)
        "နိုင်ငံခြားသတင်းဌာနတွေ ဘာပြောလဲ"  (what do foreign news agencies say)

    both returned the source list instead of searching. A topical word now needs
    a counting/enumeration form beside it to count as a question about the
    system.
    """
    if not query:
        return False
    low = query.lower()

    if any(marker in low for marker in _STRONG_META_MARKERS):
        return True

    if any(topic in low for topic in _TOPIC_WORDS):
        return any(form in low for form in _COUNTING_FORMS)

    return False


def get_sources_report() -> str:
    """စနစ်ထဲတွင် ထည့်သွင်းထားသော သတင်းဌာနများ စာရင်းကို သပ်ရပ်စွာ ပြသခြင်း"""
    np_list = "\n".join([f"  • {p}" for p in MONITORED_SOURCES["newspapers"]])
    ind_list = "\n".join([f"  • {m}" for m in MONITORED_SOURCES["independent_media"]])
    total_count = len(MONITORED_SOURCES["newspapers"]) + len(MONITORED_SOURCES["independent_media"])

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 **စနစ်အတွင်း ချိတ်ဆက်ထားသော သတင်းဌာနများ**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 **စုစုပေါင်း သတင်းမီဒီယာ: ({total_count}) ခု**\n\n"
        "🏛 **နိုင်ငံပိုင် သတင်းစာများ (Newspapers):**\n"
        f"{np_list}\n\n"
        "📡 **လွတ်လပ်သော သတင်းမီဒီယာများ (Independent Media):**\n"
        f"{ind_list}\n\n"
        "💡 **သိမှတ်ဖွယ်ရာ:**\n"
        "• သတင်းစာသီးသန့် မေးမြန်းချက်များကို `မြန်မာ့အလင်း` နှင့် `ကြေးမုံ` မှ ရှာဖွေပေးပါသည်\n"
        "• သတင်းဌာနစုံ Cross-Check ပြုလုပ်လိုပါက `/compare <ခေါင်းစဉ်>` ဟု အသုံးပြုနိုင်ပါသည်\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def get_system_status(supabase_client: Optional[Client]) -> str:
    """ဒေတာဘေ့စ်အတွင်းရှိ Live Data အရေအတွက်နှင့် အခြေအနေများကို စစ်ဆေးပြသခြင်း"""
    total_articles = 0
    total_numbers = 0
    latest_date = "မသိရှိပါ"

    if supabase_client:
        try:
            res_art = supabase_client.from_("articles").select("article_id", count="exact").limit(1).execute()
            total_articles = getattr(res_art, "count", 0) or 0
        except Exception as e:
            logger.warning("Status articles query warning: %s", e)

        try:
            res_num = supabase_client.from_("newspaper_numbers").select("id", count="exact").limit(1).execute()
            total_numbers = getattr(res_num, "count", 0) or 0
        except Exception as e:
            logger.warning("Status numbers query warning: %s", e)

        try:
            res_date = supabase_client.from_("articles").select("issue_date").order("issue_date", desc=True).limit(1).execute()
            if res_date and getattr(res_date, "data", None):
                latest_date = res_date.data[0].get("issue_date", "မသိရှိပါ")
        except Exception as e:
            logger.warning("Status latest date query warning: %s", e)

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ **သတင်းဒေတာဘေ့စ် လက်ရှိအခြေအနေ (System Status)**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 **နောက်ဆုံး ရောက်ရှိထားသော သတင်းစာရက်စွဲ:** {latest_date}\n"
        f"📰 **စုစုပေါင်း သတင်းဆောင်းပါး အရေအတွက်:** {total_articles} ပုဒ်\n"
        f"📊 **တိကျသော ဈေးနှုန်း/ကိန်းဂဏန်း မှတ်တမ်း:** {total_numbers} ခု\n"
        f"🤖 **စနစ်လည်ပတ်မှု အခြေအနေ:** ပုံမှန်လည်ပတ်နေပါသည် (Active ✅)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# 3. GENERAL CONVERSATION FALLBACK
# ============================================================

def handle_general_ai_conversation(
    user_query: str,
    genai_client: Optional[genai.Client],
    gemini_model: Optional[str] = None,
) -> str:
    """
    သတင်းစာဒေတာဘေ့စ်နှင့် မသက်ဆိုင်သော အထွေထွေမေးခွန်းများကို
    'သတင်းစာထဲ မပါပါ' ဟု မပြောဘဲ သဘာဝကျကျ အသိဉာဏ်ရှိစွာ ဖြေကြားပေးခြင်း
    """
    if not genai_client:
        return "မင်္ဂလာပါခင်ဗျာ၊ ကျွန်တော်သည် မြန်မာ့သတင်းစောင့်ကြည့်ရေး ဉာဏ်ရည်တု လက်ထောက် ဖြစ်ပါသည်။"

    prompt = f"""
You are the Myanmar Intelligent Newsroom Assistant.
The user asked a general conversational or informational question that is not a search for today's newspaper articles.

User Question: {user_query}

Instructions:
1. Answer politely, helpfully, and naturally in clean Burmese.
2. If it's a greeting, greet them warmly and introduce your capabilities (tracking newspaper news, comparing fuel/gold prices, cross-checking media).
3. Do NOT say 'ပေးထားသော သတင်းစာအထောက်အထားတွင် မပါပါ'.
4. Keep the answer concise and professional.
"""
    try:
        res = generate_content_with_fallback(
            genai_client,
            contents=prompt,
            models=resolve_models(gemini_model),
        )
        return (res.text or "").strip()
    except Exception as e:
        logger.error("General conversation error: %s", e)
        return "မင်္ဂလာပါခင်ဗျာ။ သတင်းစာပါ အကြောင်းအရာများ၊ စက်သုံးဆီ/ရွှေဈေးနှုန်းများနှင့် သတင်းဌာနစုံ နှိုင်းယှဉ်ချက်များကို မေးမြန်းနိုင်ပါသည်ခင်ဗျာ။"
