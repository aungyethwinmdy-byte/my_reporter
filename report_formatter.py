"""
================================================================
REPORT FORMATTER MODULE
Handles all Telegram formatting, Burmese numbers, and card dashboards.
Supports: Fuel, Gold, Currency, Commodities, and Statistics.
================================================================
"""

import re

BURMESE_DIGIT_MAP = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
TO_BURMESE_MAP = str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉")


def normalize_digits(text: str) -> str:
    """မြန်မာဂဏန်းများကို အင်္ဂလိပ်ဂဏန်းအဖြစ် ပြောင်းလဲခြင်း"""
    if text is None:
        return ""
    return str(text).translate(BURMESE_DIGIT_MAP)


def to_burmese_digits(num_str) -> str:
    """အင်္ဂလိပ်ဂဏန်းများကို မြန်မာဂဏန်း ပြောင်းလဲခြင်း (ကော်မာ ပါဝင်ပြီး .0 ဖြုတ်သည်)"""
    if num_str is None:
        return ""
    if isinstance(num_str, (int, float)):
        val = num_str
        if isinstance(val, float) and val.is_integer():
            val = int(val)
        if isinstance(val, int):
            formatted = f"{val:,}"
        else:
            formatted = f"{val:,.2f}".rstrip("0").rstrip(".")
        return formatted.translate(TO_BURMESE_MAP)

    s = str(num_str).strip()
    if not s:
        return ""

    norm = s.translate(BURMESE_DIGIT_MAP).replace(",", "").strip()
    try:
        f = float(norm)
        if f.is_integer():
            val = int(f)
            formatted = f"{val:,}"
        else:
            formatted = f"{f:,.2f}".rstrip("0").rstrip(".")
        return formatted.translate(TO_BURMESE_MAP)
    except ValueError:
        return s.translate(TO_BURMESE_MAP)


def clean_number_value(val_str) -> float | None:
    """ဂဏန်းစာသားမှ Float တန်ဖိုး ထုတ်ယူခြင်း"""
    if val_str is None:
        return None
    s = normalize_digits(str(val_str)).replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            return None
    return None


def simplify_item_name(name: str) -> str:
    """ဇယားအတွင်း အမည်များ တိုတိုရှင်းရှင်း ဖြစ်စေရန် ပြုပြင်ခြင်း"""
    if not name:
        return ""
    s = (
        name.replace("ရည်ညွှန်းလက်ကားဈေးနှုန်းများ", "")
        .replace("ရည်ညွှန်းလက်ကားဈေး", "")
        .replace("ရည်ညွှန်းဈေးနှုန်း", "")
        .replace("ရည်ညွှန်းဈေး", "")
        .strip()
    )
    # Special naming beautification
    if s == "Diesel":
        return "Diesel (ရိုးရိုးဒီဇယ်)"
    if s == "Premium Diesel":
        return "Premium Diesel (ပရီမီယမ်)"
    if "စံချိန်မီရွှေ" in s:
        return "စံချိန်မီရွှေ (၁၆ ပဲရည်)"
    return s or name


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

    is_comparison = len(dates) == 2
    d_yesterday, d_today = (dates[0], dates[1]) if is_comparison else (None, dates[0])

    # Group by context / item name
    grouped = {}
    source_headlines = set()

    for r in data_rows:
        raw_ctx = r.get("context") or r.get("headline") or ""
        ctx = simplify_item_name(raw_ctx)
        dt = str(r.get("publication_date"))
        if ctx not in grouped:
            grouped[ctx] = {"unit": r.get("unit") or "ကျပ်", "dates": {}}
        grouped[ctx]["dates"][dt] = r
        if r.get("headline"):
            source_headlines.add(r["headline"])

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

    total_diff = 0
    has_diff = False

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
                    total_diff += diff
                    has_diff = True
                    diff_int = int(diff) if diff.is_integer() else diff
                    diff_mm = to_burmese_digits(str(abs(diff_int)))

                    if diff > 0:
                        diff_display = f"🔺 +{diff_mm} {unit} (တက်)"
                    elif diff < 0:
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

    # သုံးသပ်ချက် အကျဉ်း
    if has_diff:
        if total_diff > 0:
            trend = "ဈေးနှုန်း အနည်းငယ် ပြန်လည်မြင့်တက်ခဲ့ပါသည်။"
        elif total_diff < 0:
            trend = "ဈေးနှုန်း အနည်းငယ် ပြန်လည်ကျဆင်းခဲ့ပါသည်။"
        else:
            trend = "ဈေးနှုန်း ပြောင်းလဲမှုမရှိဘဲ တည်ငြိမ်နေပါသည်။"
    else:
        trend = "သတင်းစာပါ ကိန်းဂဏန်းများအတိုင်း တိကျစွာ ဖော်ပြထားပါသည်။"

    source_text = (
        list(source_headlines)[0]
        if source_headlines
        else "သတင်းစာ စာမျက်နှာ (၂) ရည်ညွှန်းလက်ကားဈေးနှုန်းများ"
    )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 **အခြေအနေ:** {trend}")
    lines.append(f"🏛 **အရင်းအမြစ်:** {source_text}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)
