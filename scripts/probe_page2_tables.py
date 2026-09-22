"""Read-only probe: why does `extract_page2_tables` return nothing?

The page-2 fuel/gold box is the highest-value structured data the pipeline
produces, and the extractor reports zero rows for *both* current newspapers.
This separates the possible reasons:

  - the box is not detected as a table at all
  - the box is detected, but its text layer mangles the keywords
  - the box is detected and readable, but the keyword list is wrong

    python scripts/probe_page2_tables.py <paper.pdf> [...]
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import pdfplumber  # noqa: E402

import ingest_engine as ie  # noqa: E402

PUA = re.compile(r"[\ue000-\uf8ff]")
CID = re.compile(r"[(]cid:\d+[)]")
GLYPH = re.compile(r"/g\d+")

FUEL_KEYWORDS = ["စက်သုံးဆီ", "ရည်ညွှန်းလက်ကား", "Octane", "Diesel", "ဒီဇယ်"]
GOLD_KEYWORDS = ["ရွှေ", "ရည်ညွှန်း"]


def flatten(table):
    return "\n".join(
        " | ".join(str(c).strip() for c in row if c) for row in table if row
    )


for path in sys.argv[1:]:
    print("=" * 78)
    print(f"{os.path.basename(path)}  ({os.path.getsize(path):,} bytes)")
    print(f"extract_page2_tables() -> {len(ie.extract_page2_tables(path))} row(s)")

    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) < 2:
            print("  fewer than 2 pages")
            continue
        page = pdf.pages[1]
        text = page.extract_text() or ""
        tables = page.extract_tables() or []

    print(f"\n  page 2: {len(text):,} chars, "
          f"pua={len(PUA.findall(text))}, cid={len(CID.findall(text))}, "
          f"glyph={len(GLYPH.findall(text))}")
    print("  keyword presence in the PAGE TEXT:")
    for k in FUEL_KEYWORDS + GOLD_KEYWORDS:
        print(f"     {k!r:24s} {k in text}")

    print(f"\n  tables detected on page 2: {len(tables)}")
    for i, table in enumerate(tables):
        flat = flatten(table)
        fuel = [k for k in FUEL_KEYWORDS if k in flat]
        gold = [k for k in GOLD_KEYWORDS if k in flat]
        print(f"   [{i}] {len(flat):5d} chars  pua={len(PUA.findall(flat)):3d} "
              f"cid={len(CID.findall(flat)):3d}")
        print(f"        fuel keywords matched: {fuel or 'NONE'}")
        print(f"        gold keywords matched: {gold or 'NONE'}")
        print(f"        head: {flat[:110]!r}")

    print("\n  reading: a box that is not in `tables` cannot match at all; a box "
          "whose keywords are mangled into PUA/(cid:) cannot match either.")
