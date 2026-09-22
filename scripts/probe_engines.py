"""Read-only probe #4: compare all three text engines on the live PDFs.

The question: is the glyph-code text layer a *pypdf* limitation (missing
fontTools, so pypdf cannot map the CFF Type1 glyph indices to Unicode) or a
property of the file itself (so every engine fails and the page must go to the
Gemini vision pass)?

Run from the repo root::

    python scripts/probe_engines.py <mal.pdf> <km.pdf>
"""

import re
import sys

BURMESE = re.compile(r"[\u1000-\u109f]")
GLYPH_TOKEN = re.compile(r"/g\d+")


def glyph_ratio(t):
    toks = GLYPH_TOKEN.findall(t)
    if not t or not toks:
        return 0.0
    return sum(len(x) for x in toks) / len(t)


def score(label, pages):
    """pages: list of (no, text)"""
    if not pages:
        print(f"  {label:22s} -> NO OUTPUT")
        return
    with_burmese = sum(1 for _, t in pages if t and BURMESE.search(t))
    pure_glyph = sum(
        1 for _, t in pages
        if t and t.strip() and not BURMESE.search(t) and glyph_ratio(t) >= 0.5
    )
    total_chars = sum(len(t or "") for _, t in pages)
    print(
        f"  {label:22s} -> pages={len(pages):3d}  "
        f"with_burmese={with_burmese:3d}  pure_glyph={pure_glyph:3d}  "
        f"chars={total_chars:,}"
    )
    sample = next((t for _, t in pages if t and BURMESE.search(t)), None)
    if sample:
        print(f"      sample: {sample[:110]!r}")
    else:
        s = next((t for _, t in pages if t and t.strip()), "")
        print(f"      sample: {s[:110]!r}")


def by_pypdf(path):
    import pypdf
    r = pypdf.PdfReader(path)
    return [(i + 1, p.extract_text() or "") for i, p in enumerate(r.pages)]


def by_fitz(path):
    import fitz
    doc = fitz.open(path)
    out = [(i + 1, p.get_text()) for i, p in enumerate(doc)]
    doc.close()
    return out


def by_pdfplumber(path):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(pdf.pages)]


for path in sys.argv[1:]:
    print("=" * 72)
    print(path)
    for label, fn in (
        ("pypdf (+fontTools)", by_pypdf),
        ("PyMuPDF/fitz", by_fitz),
        ("pdfplumber", by_pdfplumber),
    ):
        try:
            score(label, fn(path))
        except Exception as e:
            print(f"  {label:22s} -> ERROR {type(e).__name__}: {e}")
