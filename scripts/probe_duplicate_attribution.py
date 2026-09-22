"""Read-only probe #7: is the article duplication a glyph artefact?

An earlier note attributed the repeated (paper, date, headline) keys to
"per-page boilerplate". That was not verified. This splits the duplication into
rows with a glyph-code body and rows with real text, so the attribution is
based on a measurement rather than an impression.

    python scripts/probe_duplicate_attribution.py
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

import ingest_engine as ie  # noqa: E402

PAGE = 1000


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

rows = fetch_all(
    "articles", "id,newspaper_name,issue_date,page_no,headline,body_text"
)
print(f"articles rows: {len(rows)}")

for r in rows:
    r["_glyph"] = ie.looks_like_glyph_noise(r.get("body_text") or "")

glyph_rows = [r for r in rows if r["_glyph"]]
clean_rows = [r for r in rows if not r["_glyph"]]
print(f"glyph rows: {len(glyph_rows)}   clean rows: {len(clean_rows)}")


def duplication(label, subset):
    keys = Counter(
        (
            (r.get("newspaper_name") or "").strip(),
            r.get("issue_date"),
            (r.get("headline") or "").strip(),
        )
        for r in subset
    )
    repeated = {k: c for k, c in keys.items() if c > 1}
    redundant = sum(c - 1 for c in repeated.values())
    print(f"\n{label}")
    print(f"  rows:                  {len(subset)}")
    print(f"  repeated keys:         {len(repeated)}")
    print(f"  redundant rows:        {redundant}")
    # how many of the repeated keys are a repeated *masthead* (headline == paper)
    masthead = sum(
        1 for (paper, _, head) in repeated if head and head.strip() == (paper or "").strip()
    )
    print(f"  repeated keys whose headline == the paper name: {masthead}")
    for k, c in sorted(repeated.items(), key=lambda kv: -kv[1])[:6]:
        print(f"     {c:4d}x  {k[0]} {k[1]}  {k[2][:46]!r}")
    return redundant


total_redundant = duplication("ALL rows", rows)
clean_redundant = duplication("CLEAN rows only (glyph rows removed)", clean_rows)

print("\n" + "=" * 70)
print(f"redundant rows, all rows:    {total_redundant}")
print(f"redundant rows, clean only:  {clean_redundant}")
print(f"=> attributable to glyph rows: {total_redundant - clean_redundant}")

# A stricter identity: same page AND same body.
strict = Counter(
    (
        (r.get("newspaper_name") or "").strip(),
        r.get("issue_date"),
        r.get("page_no"),
        (r.get("headline") or "").strip(),
        (r.get("body_text") or "").strip(),
    )
    for r in rows
)
print(f"\nstrict (paper,date,page,headline,body) redundant rows: "
      f"{sum(c - 1 for c in strict.values() if c > 1)}")
strict_clean = Counter(
    (
        (r.get("newspaper_name") or "").strip(),
        r.get("issue_date"),
        r.get("page_no"),
        (r.get("headline") or "").strip(),
        (r.get("body_text") or "").strip(),
    )
    for r in clean_rows
)
print(f"strict, clean rows only:                             "
      f"{sum(c - 1 for c in strict_clean.values() if c > 1)}")
