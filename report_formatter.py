"""
================================================================
REPORT FORMATTER MODULE
Handles all Telegram formatting, Burmese numbers, and card dashboards.
Supports: Fuel, Gold, Currency, Commodities, and Statistics.
================================================================
"""

import re

from number_utils import (
    BURMESE_DIGIT_MAP,
    collapse_grouped_thousands,
    normalize_burmese_numerals,
)

TO_BURMESE_MAP = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")


def normalize_digits(text: str) -> str:
    """မြန်မာဂဏန်းများကို အင်္ဂလိပ်ဂဏန်းအဖြစ် ပြောင်းလဲခြင်း"""
    if text is None:
        return ""
    return str(text).translate(BURMESE_DIGIT_MAP)


def _format_decimal_string(digits: str) -> str:
    """Group an already-normalized decimal *string* with thousands separators.

    Input is ASCII digits with at most one dot, e.g. "1500.50". The caller's
    fractional digits are preserved verbatim: the stored ``value`` is a string
    produced by ``auto_numeric_extractor.clean_number``, and rounding it through
    float first would drop a meaningful trailing zero (1500.50 -> "1,500.5",
    i.e. five hundred pyas becomes five hundred ... five). Prices must render
    exactly as reported.
    """
    sign = ""
    if digits[:1] in "+-":
        sign, digits = digits[0], digits[1:]
    whole, dot, frac = digits.partition(".")
    whole = whole.lstrip("0") or "0"
    grouped = f"{int(whole):,}" if whole.isdigit() else whole
    return f"{sign}{grouped}.{frac}" if dot else f"{sign}{grouped}"


def _format_decimal(value: float) -> str:
    """Format a non-integer float with up to 2 decimals, keeping the .0 rule.

    Floats carry no notion of a significant trailing zero (``1500.50`` is just
    ``1500.5``), so this path can only trim. It exists for callers that pass a
    real float; the string path preserves digits exactly.
    """
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def to_burmese_digits(num_str) -> str:
    """အင်္ဂလိပ်ဂဏန်းများကို မြန်မာဂဏန်း ပြောင်းလဲခြင်း (ကော်မာ ပါဝင်ပြီး .0 ဖြုတ်သည်)"""
    if num_str is None:
        return ""
    if isinstance(num_str, bool):
        # bool is an int subclass; "၁" for True would be nonsense.
        return str(num_str)
    if isinstance(num_str, int):
        return f"{num_str:,}".translate(TO_BURMESE_MAP)
    if isinstance(num_str, float):
        if num_str.is_integer():
            return f"{int(num_str):,}".translate(TO_BURMESE_MAP)
        return _format_decimal(num_str).translate(TO_BURMESE_MAP)

    s = str(num_str).strip()
    if not s:
        return ""

    norm = s.translate(BURMESE_DIGIT_MAP).replace(",", "").strip()
    # Only treat it as a plain number when the *entire* string is one, so a
    # value like "2500-2600" or "1500 ကျပ်" falls through to the raw passthrough
    # instead of being silently rewritten.
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", norm):
        return _format_decimal_string(norm).translate(TO_BURMESE_MAP)
    return s.translate(TO_BURMESE_MAP)


def clean_number_value(val_str) -> float | None:
    """ဂဏန်းစာသားမှ Float တန်ဖိုး ထုတ်ယူခြင်း

    Separator ဖြုတ်ခြင်းကို ``number_utils.collapse_grouped_thousands`` နှင့်
    မျှဝေထားသည်။ ဤဘက်တွင် အရေးကြီးသည့်အချက်မှာ dashboard က ``value`` မရှိပါက
    ``original_value`` သို့ ပြန်ကျပြီး ထို field ကို မော်ဒယ်မှ တိုက်ရိုက်သိမ်းထားခြင်း
    ဖြစ်သည် — ဆိုလိုသည်မှာ "၇၊၁၅၀၊၀၀၀" ကဲ့သို့ မူရင်းမြန်မာစာသား အတိအကျဖြစ်သည်။
    "," သာဖြုတ်ပြီး ဘယ်ဘုံးဆုံးဂဏန်းကို ယူခြင်းက ၇.၁၅ သန်း ကျပ်ဈေးကို 7.0
    အဖြစ် ပြသခဲ့သည်။
    """
    if val_str is None:
        return None
    s = collapse_grouped_thousands(
        normalize_burmese_numerals(val_str).replace(",", "").strip()
    )
    match = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


_PRICE_LABEL_RE = re.compile(
    r"(ရည်ညွှန်းလက်ကားဈေးနှုန်းများ|ရည်ညွှန်းလက်ကားဈေး|ရည်ညွှန်းဈေးနှုန်း|ရည်ညွှန်းဈေး)"
)


def simplify_item_name(name: str) -> str:
    """ဇယားအတွင်း အမည်များ တိုတိုရှင်းရှင်း ဖြစ်စေရန် ပြုပြင်ခြင်း

    Stripping is done with a single regex instead of a chained ``.replace()``.
    The chain was order-sensitive: ``ရည်ညွှန်းဈေး`` is a suffix of
    ``ရည်ညွှန်းဈေးနှုန်း``, so once the longer pattern had consumed the text the
    shorter replacement had nothing left to match. It also missed the case where
    a *composite* name ends in a longer label but the shorter label is what the
    author actually wrote.

    When a name is nothing *but* a price label the original is returned intact
    via ``s or name``. That is deliberate: returning "" would make every such
    row collapse into a single empty group key and silently merge unrelated
    items. Distinct labels keep distinct groups.
    """
    if not name:
        return ""
    s = _PRICE_LABEL_RE.sub("", name).strip()
    # Special naming beautification
    if s == "Diesel":
        return "Diesel (ရိုးရိုးဒီဇယ်)"
    if s == "Premium Diesel":
        return "Premium Diesel (ပရီမီယမ်)"
    if "စံချိန်မီရွှေ" in s:
        return "စံချိန်မီရွှေ (၁၆ ပဲရည်)"
    return s or name


def _pick_source_headline(headlines: list[str]) -> str:
    """Choose the source headline for the footer, deterministically.

    The rows arrive from Supabase in whatever order the query happened to
    return, which is not stable across calls. Picking ``headlines[0]`` made the
    footer newspaper vary for identical data, so a report could cite မြန်မာ့အလင်း
    on one run and ကြေးမုံ on the next. Sort first so the same input always
    produces the same citation.
    """
    return sorted(headlines)[0]


def get_category_icon(title: str, items: list[str]) -> str:
    """အမျိုးအစားအလိုက် လိုက်ဖက်သော Emoji ရွေးချယ်ခြင်း"""
    joined = f"{title} {' '.join(items)}".lower()
    if any(k in joined for k in ["octane", "diesel", "ဆီ", "ဓာတ်ဆီ", "စက်သုံးဆီ"]):
        return "⛽"
    if "ရွှေ" in joined:
        return "🪙"
    if any(k in joined for k in ["ဒေါ်လာ", "ငွေလဲ", "usd"]):
        return "💵"
    if any(k in joined for k in ["စပါး", "ဆန်", "ပဲ"]):
        return "🌾"
    return "📊"


def format_numeric_dashboard(
    data_rows: list[dict],
    dates: list[str],
    user_query: str = "",
    title_override: str = "",
) -> str:
    """
    ကိန်းဂဏန်းနှင့် ဈေးနှုန်းများကို ဖတ်ရရှင်းလင်းသော Card Dashboard အဖြစ် ပြောင်းလဲပေးသည့် အဓိက Function
    """
    if not data_rows:
        return ""

    # ရက်စွဲနှစ်ခုထက် ပိုပါက comparison အဖြစ် မယူပါ — မူလက len(dates) == 2 ကိုသာ
    # စစ်သောကြောင့် ရက်စွဲ ၃ ခု ပါလာပါက တိတ်တဆိတ် single-day branch ထဲ ရောက်သွားပြီး
    # ဒုတိယနှင့် တတိယရက်စွဲများ လုံးဝ ပျောက်ဆုံးသွားပါသည်။
    if not dates:
        return ""
    if len(dates) > 2:
        # Keep the two most recent (callers pass dates oldest -> newest).
        d_yesterday, d_today = dates[-2], dates[-1]
        is_comparison = True
    elif len(dates) == 2:
        d_yesterday, d_today = dates[0], dates[1]
        is_comparison = True
    else:
        d_yesterday, d_today = None, dates[0]
        is_comparison = False

    # Group by context / item name
    grouped = {}
    source_headlines = []

    for r in data_rows:
        raw_ctx = r.get("context") or r.get("headline") or ""
        ctx = simplify_item_name(raw_ctx)
        dt = str(r.get("publication_date"))
        if ctx not in grouped:
            grouped[ctx] = {"unit": r.get("unit") or "ကျပ်", "dates": {}}
        grouped[ctx]["dates"][dt] = r
        # Preserve insertion order and de-duplicate. The old code collected a
        # set and then did list(source_headlines)[0], so which newspaper got
        # credited as the source depended on Python's hash ordering of the
        # headline strings — the same data could print MAL or KM across runs.
        headline = r.get("headline")
        if headline and headline not in source_headlines:
            source_headlines.append(headline)

    icon = get_category_icon(title_override, list(grouped.keys()))
    title = title_override or "စက်သုံးဆီ ရည်ညွှန်းလက်ကားဈေးနှုန်းများ"
    date_header = (
        f"{d_yesterday} (မနေ့က) နှင့် {d_today} (ဒီနေ့)"
        if is_comparison
        else f"{d_today}"
    )

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"{icon} **{title}**",
        f"🗓 ရက်စွဲ: {date_header}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n",
    ]

    # Direction is tallied per item rather than by summing raw deltas. Summing
    # added gold's +200 (on a 3,000,000 base) to fuel's -100 and reported "ဈေးနှုန်း
    # မြင့်တက်" even though the fuel price the user asked about had fallen. The
    # magnitudes are not comparable across commodities, so only the sign is.
    risen = fallen = 0

    for ctx, info in grouped.items():
        unit = info["unit"]
        if is_comparison:
            y_rec = info["dates"].get(d_yesterday)
            t_rec = info["dates"].get(d_today)

            y_raw = (
                y_rec.get("original_value")
                if y_rec and y_rec.get("original_value") is not None
                else (y_rec.get("value") if y_rec else None)
            )
            y_str = to_burmese_digits(y_raw) if y_raw is not None else "မပါရှိပါ"

            t_raw = (
                t_rec.get("original_value")
                if t_rec and t_rec.get("original_value") is not None
                else (t_rec.get("value") if t_rec else None)
            )
            t_str = to_burmese_digits(t_raw) if t_raw is not None else "မပါရှိပါ"

            diff_display = "မပြောင်းလဲ"
            if y_rec and t_rec:
                y_val = clean_number_value(y_rec.get("value"))
                if y_val is None and y_rec.get("original_value"):
                    y_val = clean_number_value(y_rec.get("original_value"))

                t_val = clean_number_value(t_rec.get("value"))
                if t_val is None and t_rec.get("original_value"):
                    t_val = clean_number_value(t_rec.get("original_value"))

                if y_val is not None and t_val is not None:
                    diff = t_val - y_val
                    diff_int = int(diff) if diff.is_integer() else diff
                    diff_mm = to_burmese_digits(str(abs(diff_int)))

                    if diff > 0:
                        risen += 1
                        diff_display = f"🔺 +{diff_mm} {unit} (တက်)"
                    elif diff < 0:
                        fallen += 1
                        diff_display = f"🔻 -{diff_mm} {unit} (ကျ)"
                    else:
                        diff_display = "➖ မပြောင်းလဲ"

            lines.append(
                f"🔹 **{ctx}**\n"
                f"   • မနေ့က: {y_str} {unit}\n"
                f"   • ဒီနေ့  : {t_str} {unit}\n"
                f"   • အပြောင်းအလဲ: **{diff_display}**\n"
            )
        else:
            rec = info["dates"].get(d_today)
            val_raw = (
                rec.get("original_value")
                if rec and rec.get("original_value") is not None
                else (rec.get("value") if rec else None)
            )
            val_str = to_burmese_digits(val_raw) if val_raw is not None else "မပါရှိပါ"
            lines.append(f"🔹 **{ctx}**: {val_str} {unit}")

    # သုံးသပ်ချက် အကျဉ်း — count which way the individual items moved rather
    # than summing incomparable magnitudes.
    if risen or fallen:
        if risen and fallen:
            trend = (
                f"ပစ္စည်းအချို့ ဈေးတက် ({risen} မျိုး)၊ အချို့ ဈေးကျ ({fallen} မျိုး) "
                "ဖြစ်ပေါ်ခဲ့ပါသည်။"
            )
        elif risen:
            trend = f"ပစ္စည်း {risen} မျိုး၏ ဈေးနှုန်း ပြန်လည်မြင့်တက်ခဲ့ပါသည်။"
        else:
            trend = f"ပစ္စည်း {fallen} မျိုး၏ ဈေးနှုန်း ပြန်လည်ကျဆင်းခဲ့ပါသည်။"
    else:
        trend = "သတင်းစာပါ ကိန်းဂဏန်းများအတိုင်း တိကျစွာ ဖော်ပြထားပါသည်။"

    source_text = (
        _pick_source_headline(source_headlines)
        if source_headlines
        else "သတင်းစာ စာမျက်နှာ (၂) ရည်ညွှန်းလက်ကားဈေးနှုန်းများ"
    )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 **အခြေအနေ:** {trend}")
    lines.append(f"🏛 **အရင်းအမြစ်:** {source_text}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)
