"""Read-only probe: quantify font-glyph-code rows in the `articles` corpus.

A PDF whose text layer is font *glyph indices* rather than Unicode extracts as
runs like ``/g167/g136/g3/g3``. Those pages are non-blank, so the current
acceptance test in ``ingest_engine.extract_text_from_pdf``
(``if any(t[1].strip() for t in pages_text)``) accepts them, and the garbage is
then fed to Gemini as if it were Burmese text.

Run from the repo root so ``load_dotenv()`` finds ``.env``::

    python scripts/probe_glyph_rows.py
"""

import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from env_config import get_supabase_credentials  # noqa: E402
from supabase import create_client  # noqa: E402

# A glyph-code run: "/g" followed by digits, repeated. One or two of these can
# appear by accident; a *run* of them means the text layer is not Unicode.
GLYPH_TOKEN = re.compile(r"/g\d+")
BURMESE = re.compile(r"[\u1000-\u109f]")

PAGE = 1000


def glyph_ratio(text: str) -> float:
    """Fraction of the string consumed by glyph-code tokens."""
    if not text:
        return 0.0
    tokens = GLYPH_TOKEN.findall(text)
    if not tokens:
        return 0.0
    consumed = sum(len(t) for t in tokens)
    return consumed / len(text)


def looks_garbled(text: str) -> bool:
    """True when the text is dominated by glyph codes and has no Burmese."""
    if not text:
        return False
    if BURMESE.search(text):
        return False
    return glyph_ratio(text) >= 0.5


def fetch_all(table: str, columns: str):
    rows = []
    start = 0
    while True:
        resp = (
            client.table(table)
            .select(columns)
            .range(start, start + PAGE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


creds = get_supabase_credentials()
if not creds:
    print("no supabase credentials")
    raise SystemExit(1)
url, key = creds
client = create_client(url, key)

rows = fetch_all(
    "articles",
    "id,newspaper_name,issue_date,page_no,headline,body_text",
)
print(f"total articles rows: {len(rows)}")

garbled = [r for r in rows if looks_garbled(r.get("body_text") or "")]
print(f"garbled body_text rows: {len(garbled)}  ({len(garbled)/max(len(rows),1):.1%})")

garbled_headline = [r for r in garbled if looks_garbled(r.get("headline") or "")]
print(f"  ... headline ALSO garbled: {len(garbled_headline)}")
print(f"  ... headline readable but body garbled: {len(garbled) - len(garbled_headline)}")

print("\nby newspaper / date (garbled / total):")
per = Counter()
tot = Counter()
for r in rows:
    k = (r.get("newspaper_name"), r.get("issue_date"))
    tot[k] += 1
    if looks_garbled(r.get("body_text") or ""):
        per[k] += 1
for k in sorted(tot, key=lambda k: -per[k]):
    if per[k]:
        print(f"   {per[k]:4d}/{tot[k]:<4d}  {k[0]}  {k[1]}")

print("\nsample garbled bodies:")
for r in garbled[:5]:
    body = (r.get("body_text") or "")[:90]
    head = (r.get("headline") or "")[:40]
    print(f"   p{r.get('page_no')} head={head!r}")
    print(f"        body={body!r}")

# Do garbled rows coexist with readable rows for the same paper+date? If a page
# was ingested twice (once via a glyph text layer, once via vision) we would see
# both. If garbled rows are the ONLY rows for their page, vision never ran.
print("\ndo garbled rows share a (paper, date, page) with readable rows?")
good_keys = defaultdict(int)
for r in rows:
    if not looks_garbled(r.get("body_text") or ""):
        good_keys[(r.get("newspaper_name"), r.get("issue_date"), r.get("page_no"))] += 1
shadowed = 0
orphan = 0
for r in garbled:
    k = (r.get("newspaper_name"), r.get("issue_date"), r.get("page_no"))
    if good_keys.get(k):
        shadowed += 1
    else:
        orphan += 1
print(f"   garbled rows whose page ALSO has readable rows: {shadowed}")
print(f"   garbled rows on a page with NO readable rows:   {orphan}")

# How many distinct pages are affected, and how much of the corpus is unusable
# for retrieval if we keep them?
pages = {(r.get("newspaper_name"), r.get("issue_date"), r.get("page_no")) for r in garbled}
print(f"\ndistinct (paper, date, page) affected: {len(pages)}")
print("affected pages:", sorted(pages, key=lambda p: (str(p[0]), str(p[1]), p[2] or 0)))
