"""Shared numeric-text helpers for the newsroom pipeline.

Deliberately dependency-free (``re`` only) so that both the *writer*
(``auto_numeric_extractor``, which imports google-genai and supabase) and the
*reader* (``report_formatter``, which must stay importable with no credentials)
can share one implementation instead of two that drift apart.

Why this exists
---------------
Both sides used to strip only "," and then take the LEFTMOST digit run, so any
other thousands separator silently truncated the number:

    "၇၊၁၅၀၊၀၀၀"   -> 7        (Burmese comma, U+104A)
    "1 500 000"     -> 1        (plain space)
    "7\\u00a0150\\u00a0000" -> 7  (no-break space)

A 7.15-million-kyat gold price became seven kyat, with no error raised and
nothing downstream able to notice. Gold and fuel figures in these papers reach
seven digits routinely, so this was not a corner case.
"""

import re

BURMESE_DIGIT_MAP = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")

# The model writes `original_value` as prose at least as often as in digits, and
# plain digit translation mangles three common forms:
#
#   "၁၁၁ ဒသမ ၃၂"  -> "111"   should be 111.32   (ဒသမ = "decimal point")
#   "သုည ဒသမ ၁၀"   -> "10"    should be 0.10     (သုည = the word "zero")
#   "၂ဝ၂၆"         -> "2"     should be 2026     (letter ဝ typed for zero)
#
# All three were observed in production rows. The 2026 -> 2 case is a 1000x error.
_DECIMAL_WORD_RE = re.compile(r"\s*ဒသမ\s*")
_ZERO_WORD_RE = re.compile(r"သုည(?=\s*ဒသမ)")

# The letter ဝ stands in for zero only when it sits *between* digits. Blanket
# substitution would corrupt ordinary text: "ဝန်ကြီး ၂၅၀၀" would become
# "0န်ကြီး 2500" and parse as 0.
_LETTER_ZERO_RE = re.compile(r"(?<=\d)ဝ(?=\d)")

# Separators that can legitimately appear *inside* a grouped number: a plain
# space, U+00A0 (no-break space), U+202F (narrow no-break space), U+2009 (thin
# space), U+2007 (figure space) and the Burmese comma U+104A.
_SEPARATORS = " \u00a0\u202f\u2009\u2007၊"


def normalize_burmese_numerals(text: str) -> str:
    """ASCII digits, with the Burmese *word* forms of numbers resolved.

    Digits only would be enough for the ``value`` field, which the model is asked
    to emit as plain English digits — but ``original_value`` is display text and
    the reader falls back to it, so the prose forms have to survive too.
    """
    s = str(text or "").translate(BURMESE_DIGIT_MAP)
    s = _ZERO_WORD_RE.sub("0", s)
    s = _DECIMAL_WORD_RE.sub(".", s)
    return _LETTER_ZERO_RE.sub("0", s)

# A run that looks like a grouped number: 1-3 digits, then one or more
# separator + exactly-3-digit groups. The digit guards stop a match from
# beginning or ending inside a longer digit run, which is what keeps
# "၂၅၀၀၊၂၆၀၀" (two prices) and "2500 300" (two numbers) intact rather than
# being fused into one enormous wrong value.
_GROUPED_NUMBER_RE = re.compile(
    rf"(?<!\d)[+-]?\d{{1,3}}(?:[{_SEPARATORS}]\d{{3}})+(?!\d)"
)
_ANY_SEPARATOR_RE = re.compile(f"[{_SEPARATORS}]")


def collapse_grouped_thousands(text: str) -> str:
    """Remove thousands separators from numbers that are clearly grouped.

    Only runs shaped like ``1 234 567`` / ``၁၊၂၃၄၊၅၆၇`` are touched. A
    separator that is not followed by exactly three digits — a unit, or the
    start of a second number — is left alone, so ``2500 ကျပ်`` stays one number
    and ``၂၅၀၀၊၂၆၀၀`` is not merged into ``၂၅၀၀၂၆၀၀``.

    Separators only: this does not translate Burmese digits to ASCII. Both
    callers run their own digit translation first, which is what makes the
    result safe for ``float()``.
    """
    return _GROUPED_NUMBER_RE.sub(
        lambda match: _ANY_SEPARATOR_RE.sub("", match.group(0)), str(text or "")
    )
