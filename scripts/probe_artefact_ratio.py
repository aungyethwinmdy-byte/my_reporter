"""Measure the artefact ratio of every engine's output on the real PDFs.

The rule must (a) flag all three engines' output for the glyph-encoded
မြန်မာ့အလင်း issue and (b) flag nothing for the clean ကြေးမုံ issue, on all 32
pages. Anything else either leaves the bug in place or throws away good news.

    python scripts/probe_artefact_ratio.py <mal.pdf> <km.pdf>
"""

import re
import sys

import pypdf
import pdfplumber
import pymupdf

GLYPH = re.compile(r"/g\d+")
CID = re.compile(r"[(]cid:\d+[)]")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LETTER = re.compile(r"[^\W\d_]")


def parts(text):
    stripped = CID.sub("", GLYPH.sub("", text))
    artefacts = (len(text) - len(stripped)) + len(CONTROL.findall(stripped))
    return artefacts, len(LETTER.findall(stripped)), len(text)


def ratios(path):
    rows = {}
    r = pypdf.PdfReader(path)
    rows["pypdf"] = [p.extract_text() or "" for p in r.pages]
    d = pymupdf.open(path)
    rows["fitz"] = [p.get_text() for p in d]
    d.close()
    with pdfplumber.open(path) as pdf:
        rows["pdfplumber"] = [p.extract_text() or "" for p in pdf.pages]
    return rows


for path in sys.argv[1:]:
    print("=" * 78)
    print(path)
    print(f"{'engine':12s} {'pg':>3s} {'chars':>7s} {'artefact%':>10s} {'letters':>8s} {'letter%':>8s}")
    data = ratios(path)
    for engine, pages in data.items():
        worst_art = 0.0
        max_letter_pct = 0.0
        min_art_on_nonblank = 1.0
        nonblank = 0
        for i, t in enumerate(pages, start=1):
            if not t.strip():
                continue
            nonblank += 1
            a, l, n = parts(t)
            art = a / n
            worst_art = max(worst_art, art)
            max_letter_pct = max(max_letter_pct, l / n)
            min_art_on_nonblank = min(min_art_on_nonblank, art)
            if i <= 2 or (art > 0.05) != (art > 0.30):
                print(f"{engine:12s} {i:3d} {n:7d} {art:10.3f} {l:8d} {l/n:8.3f}")
        print(
            f"{engine:12s} {'ALL':>3s} nonblank={nonblank:2d} "
            f"max_artefact={worst_art:.3f} min_artefact={min_art_on_nonblank:.3f} "
            f"max_letter%={max_letter_pct:.3f}"
        )
        print()
