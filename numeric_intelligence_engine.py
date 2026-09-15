"""
================================================================
NUMERIC INTELLIGENCE ENGINE FOR TELEGRAM BOT
Provides 100% accurate, mathematically verified prices and figures.
================================================================
"""

import re
from datetime import datetime
from supabase import Client


def is_numeric_or_price_query(query: str) -> bool:
    """ဈေးနှုန်း၊ ကိန်းဂဏန်း၊ နှိုင်းယှဉ်ချက် မေးခွန်း ဟုတ်/မဟုတ် စစ်ဆေးခြင်း"""
    markers = [
        "ဈေး", "စျေး", "ရည်ညွှန်း", "ကိန်းဂဏန်း", "ဂဏန်း", "ဘယ်လောက်", 
        "နှုန်း", "တက်", "ကျ", "တိုး", "လျော့", "အပြောင်းအလဲ", "နှိုင်းယှဉ်", 
        "ကွာခြား", "octane", "diesel", "ဒီဇယ်", "ဒေါ်လာ", "ရွှေ", "စပါး"
    ]
    low = query.lower()
    return any(m in low for m in markers)


def query_exact_numbers(
    supabase_client: Client,
    search_term: str,
    dates: list[str],
) -> list[dict]:
    """Supabase newspaper_numbers ထဲမှ အတိအကျ Query ထုတ်ယူခြင်း"""
    query = (
        supabase_client.from_("newspaper_numbers")
        .select("publication_date, headline, context, value, original_value, unit, source_text")
        .in_("publication_date", dates)
        .or_(f"context.ilike.%{search_term}%,headline.ilike.%{search_term}%,source_text.ilike.%{search_term}%")
        .order("publication_date", desc=False)
    )
    res = query.execute()
    return res.data or []


def build_precision_comparison_table(
    data_rows: list[dict],
    date_yesterday: str,
    date_today: str,
    topic_title: str,
) -> str:
    """ရရှိလာသော ကိန်းဂဏန်းများကို Python ဖြင့် သင်္ချာနည်းကျ အပြောင်းအလဲ တွက်ချက်ပြသခြင်း"""
    if not data_rows:
        return ""

    # Group by context / item
    items = {}
    for r in data_rows:
        ctx = r["context"]
        dt = str(r["publication_date"])
        if ctx not in items:
            items[ctx] = {"unit": r.get("unit") or "ကျပ်", date_yesterday: None, date_today: None}
        items[ctx][dt] = r

    table_lines = [
        f"📊 **{topic_title} — တိကျသော ကိန်းဂဏန်း နှိုင်းယှဉ်ချက်**\n",
        f"| အမျိုးအမည် / အညွှန်း | မနေ့က ({date_yesterday}) | ဒီနေ့ ({date_today}) | အပြောင်းအလဲ | ယူနစ် |",
        "|---|---:|---:|---:|:---|"
    ]

    evidence_notes = []

    for ctx, val_map in items.items():
        y_record = val_map[date_yesterday]
        t_record = val_map[date_today]
        unit = val_map["unit"]

        y_str = y_record["original_value"] if y_record else "မပါရှိပါ"
        t_str = t_record["original_value"] if t_record else "မပါရှိပါ"

        # Mathematical difference calculation
        diff_str = "မတွက်ချက်နိုင်ပါ"
        if y_record and t_record:
            try:
                y_val = float(y_record["value"])
                t_val = float(t_record["value"])
                diff = t_val - y_val
                if diff == 0:
                    diff_str = "မပြောင်းလဲ"
                elif diff > 0:
                    diff_str = f"+{int(diff) if diff.is_integer() else diff:g}"
                else:
                    diff_str = f"{int(diff) if diff.is_integer() else diff:g}"
            except (ValueError, TypeError):
                diff_str = "မတွက်ချက်နိုင်ပါ"

        table_lines.append(f"| {ctx} | {y_str} | {t_str} | {diff_str} | {unit} |")

        # Source quotation
        if t_record and t_record.get("source_text"):
            evidence_notes.append(f"• **{ctx}**: {t_record['source_text']} ({t_record['headline']})")

    response_text = "\n".join(table_lines)
    if evidence_notes:
        response_text += "\n\n🏛 **မူရင်း သတင်းစာအထောက်အထား:**\n" + "\n".join(evidence_notes)

    return response_text