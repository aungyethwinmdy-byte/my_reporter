import os
from datetime import datetime
import pytz
from dotenv import load_dotenv
from supabase import create_client, Client

# .env ဖိုင်မှ Environment Variables များ ဖတ်ယူခြင်း
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Error: .env ဖိုင်ထဲတွင် SUPABASE_URL သို့မဟုတ် SUPABASE_SERVICE_ROLE_KEY မရှိပါ။")
    exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
MMT = pytz.timezone('Asia/Yangon')
today_str = datetime.now(MMT).strftime('%Y-%m-%d')

print("========================================================================")
print("🏛️ MYANMAR INTELLIGENT NEWSROOM - FULL DATABASE AUDIT & STATUS REPORT")
print(f"📅 Audit Executed Date (Myanmar Time): {datetime.now(MMT).strftime('%Y-%m-%d %H:%M:%S')}")
print("========================================================================\n")

# ----------------------------------------------------------------------
# SECTION 1: SCHEMA & RPC HEALTH CHECK
# ----------------------------------------------------------------------
print("1️⃣ [SCHEMA INTEGRITY & RPC FUNCTIONS AUDIT]")
print("------------------------------------------------------------------------")

# 1.1 Schema Check
try:
    schema_res = supabase.table("articles").select("article_id, newspaper_name, issue_date, page_no, headline, body_text").limit(1).execute()
    if schema_res.data:
        print("  ✅ Table 'articles' Structure : OK (All standard columns verified)")
    else:
        print("  ⚠️ Table 'articles' Status    : Empty Table")
except Exception as e:
    print(f"  ❌ Table 'articles' Error     : {e}")

# 1.2 RPC Check
rpc_list = [
    ("fn_search_news", {"p_query": "ရုရှား", "p_limit": 1}),
    ("fn_get_daily_briefing", {"p_date": "2026-09-04"}),
    ("fn_get_event_timeline", {"p_keyword": "ဦးညိုစော"}),
    ("fn_compare_sources", {"p_keyword": "စက်သုံးဆီ"})
]

for rpc_name, params in rpc_list:
    try:
        supabase.rpc(rpc_name, params).execute()
        print(f"  ✅ RPC `{rpc_name}` : Operational")
    except Exception as e:
        print(f"  ❌ RPC `{rpc_name}` : Error -> {e}")

# ----------------------------------------------------------------------
# SECTION 2: DATA QUALITY & TOTAL STATS
# ----------------------------------------------------------------------
print("\n2️⃣ [DATA QUALITY & NEWSPAPER BREAKDOWN]")
print("------------------------------------------------------------------------")

try:
    null_body = supabase.table("articles").select("article_id", count="exact").is_("body_text", "null").execute()
    null_date = supabase.table("articles").select("article_id", count="exact").is_("issue_date", "null").execute()
    
    alinn_total = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "မြန်မာ့အလင်း").execute()
    kyemon_total = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "ကြေးမုံ").execute()
    total_articles = (alinn_total.count or 0) + (kyemon_total.count or 0)

    print(f"  • စုစုပေါင်း သတင်းစာ အပုဒ်ရေ     : {total_articles} ပုဒ်")
    print(f"    - မြန်မာ့အလင်း စုစုပေါင်း      : {alinn_total.count or 0} ပုဒ်")
    print(f"    - ကြေးမုံ စုစုပေါင်း           : {kyemon_total.count or 0} ပုဒ်")
    print(f"  • Body text လွတ်နေသော သတင်းများ : {null_body.count or 0} ပုဒ် (Missing Data)")
    print(f"  • Issue date လွတ်နေသော သတင်းများ : {null_date.count or 0} ပုဒ် (Missing Dates)")

except Exception as e:
    print(f"  ❌ Data Quality Audit Error: {e}")

# ----------------------------------------------------------------------
# SECTION 3: DATE COVERAGE & TODAY'S INGESTION STATUS
# ----------------------------------------------------------------------
print("\n3️⃣ [NEWSPAPER DATE COVERAGE & TODAY'S INGESTION STATUS]")
print("------------------------------------------------------------------------")

try:
    earliest_res = supabase.table("articles").select("issue_date").order("issue_date", desc=False).limit(1).execute()
    latest_res = supabase.table("articles").select("issue_date").order("issue_date", desc=True).limit(1).execute()

    earliest_date = earliest_res.data[0]['issue_date'] if earliest_res.data else "N/A"
    latest_date = latest_res.data[0]['issue_date'] if latest_res.data else "N/A"

    print(f"📅 [သတင်းစာ ရက်စွဲ အကွာအဝေး Coverage]")
    print(f"  • အစောဆုံး ရရှိနိုင်သည့် ရက်စွဲ : {earliest_date}")
    print(f"  • နောက်ဆုံး ရရှိထားသည့် ရက်စွဲ : {latest_date}")

    if latest_date != "N/A":
        alinn_latest = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "မြန်မာ့အလင်း").eq("issue_date", latest_date).execute()
        kyemon_latest = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "ကြေးမုံ").eq("issue_date", latest_date).execute()
        print(f"  • ({latest_date}) ရက်နေ့ထုတ် သတင်းစာ အခြေအနေ:")
        print(f"    - မြန်မာ့အလင်း : {alinn_latest.count or 0} ပုဒ်")
        print(f"    - ကြေးမုံ       : {kyemon_latest.count or 0} ပုဒ်")

    print("\n------------------------------------------------------------------------")
    print(f"🚨 [ယနေ့ ({today_str}) သတင်းစာ တိုက်ရိုက် စိစစ်ချက်]")

    today_res = supabase.table("articles").select("article_id", count="exact").eq("issue_date", today_str).execute()
    today_count = today_res.count if today_res.count is not None else 0

    if today_count > 0:
        alinn_today = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "မြန်မာ့အလင်း").eq("issue_date", today_str).execute()
        kyemon_today = supabase.table("articles").select("article_id", count="exact").eq("newspaper_name", "ကြေးမုံ").eq("issue_date", today_str).execute()
        
        print(f"\n✅ **ယနေ့ထုတ် ({today_str}) သတင်းစာများ Database ထဲသို့ ရောက်ရှိပြီးဖြစ်ပါသည်။**")
        print(f"  • ယနေ့ စုစုပေါင်း သတင်းအရေအတွက် : {today_count} ပုဒ်")
        print(f"    - မြန်မာ့အလင်း : {alinn_today.count or 0} ပုဒ်")
        print(f"    - ကြေးမုံ       : {kyemon_today.count or 0} ပုဒ်")
        
        print("\n💡 **အကြောင်းအရင်း (Why It Exists):**")
        print("  ၁။ GitHub Actions Automated Pipeline သည် ယနေ့တွင် ပုံမှန် အလုပ်လုပ်ခဲ့သည်။")
        print("  ၂။ MDN Public Archive (mdn.gov.mm) တွင် ယနေ့ထုတ် သတင်းစာ PDF/Data တင်ပေးထားပြီးဖြစ်သည်။")
        print("  ၃။ Scraper/Parser သည် Error မရှိဘဲ Supabase သို့ အလိုအလျောက် Insert လုပ်ပေးနိုင်ခဲ့သည်။")

    else:
        print(f"\n❌ **ယနေ့ထုတ် ({today_str}) သတင်းစာများ Database ထဲတွင် မရှိသေးပါ။**")
        print(f"  • နောက်ဆုံး ရရှိထားသော သတင်းစာမှာ ({latest_date}) ရက်နေ့ထုတ် ဖြစ်ပါသည်။")
        
        print("\n💡 **မရှိသေးသည့် ဖြစ်နိုင်ခြေ အကြောင်းအရင်းများ (Why It Might Be Missing):**")
        print("  ၁။ **MDN Archive တင်ချိန် မရောက်သေးခြင်း**: MDN Public Archive (mdn.gov.mm) တွင် ယနေ့ထုတ် သတင်းစာများ မတင်ရသေးခြင်း။")
        print("  ၂။ **GitHub Actions Schedule မရောက်သေးခြင်း**: Auto Ingestion Run သည့် Cron Time (ဥပမာ - မွန်းလွဲပိုင်း) မရောက်သေးခြင်း။")
        print("  ၃။ **အစိုးရ ရုံးပိတ်ရက် ဖြစ်ခြင်း**: ယနေ့သည် အခါကြီးရက်ကြီး သို့မဟုတ် အစိုးရ ရုံးပိတ်ရက် ဖြစ်ပါက သတင်းစာများ ထွက်ရှိမည် မဟုတ်ပါ။")
        print("  ၄။ **Scraper / Network Error**: Ingestion Pipeline ကွန်နက်ရှင် ခဏပြတ်တောက်သွားခြင်း။")

except Exception as e:
    print(f"❌ Date Status Check Error: {e}")

print("\n========================================================================")
print("✨ AUDIT COMPLETED")
print("========================================================================")