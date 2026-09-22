"""Read-only probe #3: run the real ingest_engine.extract_text_from_pdf on the
two live newspaper PDFs and show what the acceptance test sees.

This settles the question "does the garbled corpus come from the text pass or
the vision pass?" by exercising the production function itself.

Run from the repo root::

    python scripts/probe_extract_live.py /tmp/glyphprobe/mal_13.pdf /tmp/glyphprobe/km_13.pdf
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import ingest_engine  # noqa: E402

GLYPH_TOKEN = re.compile(r"/g\d+")
BURMESE = re.compile(r"[\u1000-\u109f]")


def describe(path):
    print("=" * 72)
    print(f"FILE: {path}  ({os.path.getsize(path):,} bytes)")
    pages = ingest_engine.extract_text_from_pdf(path)
    print(f"pages returned: {len(pages)}")

    nonblank = [(n, t) for (n, t) in pages if t and t.strip()]
    print(f"pages that pass `if any(t[1].strip() ...)`: {len(nonblank)}")

    burmese_pages = [(n, t) for (n, t) in nonblank if BURMESE.search(t)]
    glyph_pages = []
    for n, t in nonblank:
        if BURMESE.search(t):
            continue
        toks = GLYPH_TOKEN.findall(t)
        if toks and sum(len(x) for x in toks) / len(t) >= 0.5:
            glyph_pages.append((n, t))
    print(f"  ... of those, pages containing real Burmese: {len(burmese_pages)}")
    print(f"  ... of those, pages that are PURE glyph codes: {len(glyph_pages)}")
    print(f"  ... of those, pages that are neither: {len(nonblank) - len(burmese_pages) - len(glyph_pages)}")

    if glyph_pages:
        n, t = glyph_pages[0]
        print(f"\n  first pure-glyph page = p{n}, {len(t):,} chars")
        print(f"    head: {t[:120]!r}")
        print(f"    tail: {t[-120:]!r}")

    if burmese_pages:
        n, t = burmese_pages[0]
        print(f"\n  first Burmese page = p{n}, {len(t):,} chars")
        print(f"    head: {t[:160]!r}")

    return pages


for arg in sys.argv[1:]:
    describe(arg)
