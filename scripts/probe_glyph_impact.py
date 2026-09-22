"""Read-only probe #6: downstream damage, with the real column names.

Run from the repo root::

    python scripts/probe_glyph_impact.py
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from env_config import get_supabase_credentials  # noqa: E402
from supabase import create_client  # noqa: E402

PAGE = 1000
FUEL_CONST = "ရန်ကုန်မြို့နှင့် မန္တလေးမြို့တို့အတွက် ရည်ညွှန်းလက်ကားဈေးနှုန်းများ"


def fetch_all(table, columns):
    rows, start = [], 0
    while True:
        resp = client.table(table).select(columns).range(start, start + PAGE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


url, key = get_supabase_credentials()
client = create_client(url, key)

arts = fetch_all(
    "articles", "id,newspaper_name,issue_date,page_no,page_number,headline,body_text"
)
print(f"articles rows: {len(arts)}")

# --- price-table rows -------------------------------------------------------
print("\n--- rows whose headline mentions a reference price ---")
for r in arts:
    h = (r.get("headline") or "").strip()
    if "ရည်ညွှန်းလက်ကား" in h or "ရည်ညွှန်းဈေး" in h or "ဓာတ်သတ္တု" in h:
        same = "EXACT-CONSTANT" if h == FUEL_CONST else "NOT-the-constant"
        print(
            f"   {r.get('newspaper_name')} {r.get('issue_date')} "
            f"p{r.get('page_no')}/{r.get('page_number')} [{same}]"
        )
        print(f"      headline={h!r}")
        print(f"      body={(r.get('body_text') or '')[:150]!r}")

# --- numbers volume ---------------------------------------------------------
nums = fetch_all(
    "newspaper_numbers",
    "id,headline,publication_date,value,original_value,unit,section",
)
print(f"\nnewspaper_numbers rows: {len(nums)}")
print("numbers per publication_date:")
nd = Counter(r.get("publication_date") for r in nums)
for k, c in sorted(nd.items(), key=lambda kv: str(kv[0])):
    print(f"   {c:4d}x  {k}")

print("\nnumbers whose headline mentions a reference price:")
ref = [r for r in nums if "ရည်ညွှန်း" in (r.get("headline") or "")]
print(f"   {len(ref)} of {len(nums)}")
for r in ref[:10]:
    print(f"      {r.get('publication_date')} {r.get('headline','')[:40]!r} "
          f"value={r.get('value')!r} unit={r.get('unit')!r}")

print("\nsections present:")
for k, c in Counter(r.get("section") for r in nums).most_common(15):
    print(f"   {c:4d}x  {k!r}")

# --- articles per date, per newspaper (to see which issues are fully garbled)
print("\narticles per (newspaper, date):")
ad = Counter((r.get("newspaper_name"), r.get("issue_date")) for r in arts)
for k, c in sorted(ad.items(), key=lambda kv: (str(kv[0][1]), str(kv[0][0]))):
    print(f"   {c:4d}x  {k[0]}  {k[1]}")
