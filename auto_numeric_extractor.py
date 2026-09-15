"""
================================================================
AUTO NUMERIC EXTRACTOR FOR MY_REPORTER
Extracts all prices, commodity rates, statistics into `newspaper_numbers`
================================================================
"""

import json
import logging
import re
from google import genai
from google.genai import types
from supabase import Client

logger = logging.getLogger("NumericExtractor")
BURMESE_DIGIT_MAP = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")


def clean_number(val_str: str) -> str:
    """မြန်မာဂဏန်းများကို အင်္ဂလိပ်ဂဏန်း ပြောင်းလဲပြီး ကော်မာများ ဖြုတ်ခြင်း"""
    val = (val_str or "").translate(BURMESE_DIGIT_MAP)
    # Extract only numeric tokens with optional decimal
    match = re.search(r"[-+]?\d+(?:\.\d+)?", val.replace(",", ""))
    return match.group(0) if match else val_str


def extract_numbers_from_article(
    article_text: str,
    headline: str,
    publication_date: str,
    section: str,
    genai_client: genai.Client,
    model_name: str = "gemini-2.5-flash",
) -> list[dict]:
    """သတင်းတစ်ပုဒ်ချင်းစီမှ ဈေးနှုန်းနှင့် ကိန်းဂဏန်းများကို Schema ဖြင့် တိကျစွာ ထုတ်ယူခြင်း"""
    if not genai_client or not article_text or len(article_text.strip()) < 10:
        return []

    prompt = f"""
You are a precision Numeric and Commodity Price Extractor for Myanmar News.
Analyze the following text and extract EVERY specific price, rate, quota, statistical figure, or agricultural yield mentioned.

Date: {publication_date}
Headline: {headline}
Text:
{article_text}

Extract each figure into this JSON structure:
[
  {{
    "context": "အတိအကျ အညွှန်း (ဥပမာ - Octane 92 ရည်ညွှန်းလက်ကားဈေး၊ စံချိန်မီရွှေတစ်ကျပ်သား၊ မိုးစပါးတစ်ဧကအထွက်နှုန်း၊ ရင်းနှီးမြှုပ်နှံမှုပမာဏ)",
    "value": "အင်္ဂလိပ်ဂဏန်း သီးသန့် (ဥပမာ - 3050, 7150000, 118, 140.474)",
    "original_value": "မူရင်း မြန်မာဂဏန်း (ဥပမာ - ၃,၀၅၀၊ ၇,၁၅၀,၀၀၀၊ ၁၁၈)",
    "unit": "အတိုင်းအတာယူနစ် (ဥပမာ - ကျပ်၊ တင်း၊ သန်းဒေါ်လာ၊ ဦး၊ ခု)",
    "source_text": "သတင်းထဲမှ ဂဏန်းပါဝင်သော မူရင်းစာကြောင်းတို"
  }}
]

Rules:
1. Do not invent numbers.
2. If no clear numbers or prices are mentioned, return [].
3. Return ONLY the valid JSON array.
"""

    try:
        response = genai_client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        data = json.loads(response.text)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("Numeric extraction error: %s", e)
        return []


def ingest_article_numbers(
    supabase_client: Client,
    genai_client: genai.Client,
    article_id: str,
    publication_date: str,
    headline: str,
    body_text: str,
    section: str = "အထွေထွေ",
):
    """ထုတ်ယူရရှိသော ဂဏန်းများကို `newspaper_numbers` သို့ အလိုအလျောက် သွင်းယူခြင်း"""
    extracted_numbers = extract_numbers_from_article(
        article_text=body_text,
        headline=headline,
        publication_date=publication_date,
        section=section,
        genai_client=genai_client,
    )

    if not extracted_numbers:
        return

    rows_to_insert = []
    for item in extracted_numbers:
        val = clean_number(str(item.get("value", "")))
        rows_to_insert.append({
            "article_id": article_id,
            "publication_date": publication_date,
            "headline": headline,
            "section": section,
            "context": item.get("context", ""),
            "value": val,
            "original_value": item.get("original_value", ""),
            "unit": item.get("unit", ""),
            "source_text": item.get("source_text", ""),
        })

    try:
        supabase_client.from_("newspaper_numbers").insert(rows_to_insert).execute()
        logger.info(f"✅ Ingested {len(rows_to_insert)} numbers for article: {headline[:40]}")
    except Exception as e:
        logger.error(f"❌ Failed to insert into newspaper_numbers: {e}")