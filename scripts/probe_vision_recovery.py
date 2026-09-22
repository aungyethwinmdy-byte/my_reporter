"""Live probe: does the Gemini vision pass recover real Burmese from a
glyph-encoded PDF?

The fix routes pages whose text layer is glyph indices to
``parse_articles_with_gemini_native_pdf``. That only helps if the model reads
the rendered page image instead of the (broken) embedded text layer. This probe
answers that against the real မြန်မာ့အလင်း issue whose text layer is 100% glyph
codes, using a single page so the call stays cheap.

    python scripts/probe_vision_recovery.py <one-page-glyph.pdf>
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import ingest_engine  # noqa: E402

BURMESE = re.compile(r"[\u1000-\u109f]")
GLYPH_NAME = re.compile(r"/g\d+")


def glyph_ratio(t):
    toks = GLYPH_NAME.findall(t)
    if not t or not toks:
        return 0.0
    return sum(len(x) for x in toks) / len(t)


path = sys.argv[1]
print(f"input: {path} ({os.path.getsize(path):,} bytes)")

print(f"gemini_client: {ingest_engine.gemini_client is not None}")
print(f"types:         {ingest_engine.types is not None}")

articles = ingest_engine.parse_articles_with_gemini_native_pdf(
    path, "မြန်မာ့အလင်း", "2026-09-13"
)
print(f"\narticles returned: {len(articles)}")

clean = 0
garbled = 0
for i, a in enumerate(articles):
    h = str(a.get("headline", ""))
    b = str(a.get("body_text", ""))
    ok = bool(BURMESE.search(h) and BURMESE.search(b)) and not glyph_ratio(b) >= 0.5
    clean += ok
    garbled += not ok
    print(f"\n[{i}] {'CLEAN' if ok else 'GARBLED'}  page={a.get('page_no')}")
    print(f"    headline: {h[:110]!r}")
    print(f"    body    : {b[:220]!r}")

print(f"\n=> clean: {clean}   garbled: {garbled}")

if articles:
    bodies = [str(a.get("body_text", "")) for a in articles]
    avg = sum(len(b) for b in bodies) / len(bodies)
    burmese_rows = sum(1 for b in bodies if BURMESE.search(b))
    print(f"   avg body length: {avg:.0f} chars")
    print(f"   bodies containing Burmese: {burmese_rows}/{len(bodies)}")
