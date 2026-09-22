"""Read-only probe #2: characterise the မြန်မာ့အလင်း glyph rows in detail.

Probe #1 showed the garbled rows are exclusively မြန်မာ့အလင်း, across every
page of every recent issue, and that 343/384 still carry a *readable* Burmese
headline. A readable headline beside a body with zero Burmese characters means
the headline did not come from the same text as the body — so we need to know
where each part came from before choosing a fix.

Run from the repo root::

    python scripts/probe_glyph_rows2.py
"""

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from env_config import get_supabase_credentials  # noqa: E402
from supabase import create_client  # noqa: E402

GLYPH_TOKEN = re.compile(r"/g\d+")
BURMESE = re.compile(r"[\u1000-\u109f]")
PAGE = 1000


def glyph_ratio(text: str) -> float:
    if not text:
        return 0.0
    tokens = GLYPH_TOKEN.findall(text)
    if not tokens:
        return 0.0
    return sum(len(t) for t in tokens) / len(text)


def looks_garbled(text: str) -> bool:
    if not text:
        return False
    if BURMESE.search(text):
        return False
    return glyph_ratio(text) >= 0.5


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
rows = fetch_all("articles", "id,newspaper_name,issue_date,page_no,headline,body_text")

# --- headline variety among garbled rows -------------------------------------
garbled = [r for r in rows if looks_garbled(r.get("body_text") or "")]
print(f"garbled rows: {len(garbled)}")
heads = Counter((r.get("headline") or "").strip() for r in garbled)
print(f"distinct headlines among them: {len(heads)}")
print("top headlines:")
for h, c in heads.most_common(12):
    print(f"   {c:4d}x  {h[:70]!r}")

# --- body length distribution ------------------------------------------------
lens = sorted(len(r.get("body_text") or "") for r in garbled)
if lens:
    print(
        f"\nbody length: min={lens[0]} p50={lens[len(lens)//2]} "
        f"p90={lens[int(len(lens)*0.9)]} max={lens[-1]}"
    )

# --- does the garbled body share a headline with a READABLE body? -----------
print("\nFor each garbled row, is there a readable row with the SAME headline?")
good_by_head = {}
for r in rows:
    if not looks_garbled(r.get("body_text") or ""):
        good_by_head.setdefault((r.get("headline") or "").strip(), []).append(r)
shadowed = sum(1 for r in garbled if good_by_head.get((r.get("headline") or "").strip()))
print(f"   garbled rows whose headline also has a readable body: {shadowed}")
print(f"   garbled rows with no readable counterpart:            {len(garbled) - shadowed}")

# --- is ကြေးမုံ really clean? ------------------------------------------------
print("\nrows per newspaper, and garbled count:")
by_paper = Counter()
by_paper_bad = Counter()
for r in rows:
    p = r.get("newspaper_name")
    by_paper[p] += 1
    if looks_garbled(r.get("body_text") or ""):
        by_paper_bad[p] += 1
for p, n in by_paper.most_common():
    print(f"   {by_paper_bad[p]:4d}/{n:<5d}  {p}")

# --- full text of one garbled row, and one readable row, same date ----------
print("\n" + "=" * 70)
target = next((r for r in garbled if r.get("issue_date") == "2026-09-16"), garbled[0] if garbled else None)
if target:
    print(f"FULL GARBLED ROW  {target.get('newspaper_name')} {target.get('issue_date')} p{target.get('page_no')}")
    print(f"  headline: {target.get('headline')!r}")
    print(f"  body    : {target.get('body_text')!r}")

same = next(
    (r for r in rows
     if r.get("issue_date") == "2026-09-16"
     and r.get("newspaper_name") == "ကြေးမုံ"
     and not looks_garbled(r.get("body_text") or "")),
    None,
)
if same:
    print(f"\nFULL READABLE ROW {same.get('newspaper_name')} {same.get('issue_date')} p{same.get('page_no')}")
    print(f"  headline: {same.get('headline')!r}")
    print(f"  body    : {(same.get('body_text') or '')[:300]!r}")

# --- character inventory of garbled bodies (are they ONLY /gNNN + spaces?) ---
chars = Counter()
for r in garbled:
    body = r.get("body_text") or ""
    stripped = GLYPH_TOKEN.sub("", body)
    chars.update(stripped)
print("\nnon-glyph characters present in garbled bodies (top 15):")
for ch, c in chars.most_common(15):
    print(f"   {c:6d}  {ch!r}  U+{ord(ch):04X}")
