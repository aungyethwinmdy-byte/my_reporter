import os
import re
import requests
import urllib3
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz

# SSL Warning များကို ပိတ်ထားခြင်း
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# HTTP Request Headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# သတင်းစာ ရင်းမြစ်များနှင့် သတ်မှတ်ထားသော Prefix များ
MOI_SOURCES = [
    {
        "name": "မြန်မာ့အလင်း",
        "file_prefix": "myanmaalinn",
        "prefix": "mal",
        "base_url": "https://www.moi.gov.mm",
    },
    {
        "name": "ကြေးမုံ",
        "file_prefix": "themirror",
        "prefix": "km",
        "base_url": "https://www.moi.gov.mm",
    },
    {
        "name": "The Global New Light of Myanmar",
        "file_prefix": "newlightmyanmar",
        "prefix": "nlm",
        "base_url": "https://www.moi.gov.mm",
    },
]

MONTH_MAP = {
    "01": ["jan", "january", "ဇန်နဝါရီ"],
    "02": ["feb", "february", "ဖေဖော်ဝါရီ"],
    "03": ["mar", "march", "မတ်"],
    "04": ["apr", "april", "ဧပြီ"],
    "05": ["may", "မေ"],
    "06": ["jun", "june", "ဇွန်"],
    "07": ["jul", "july", "ဇူလိုင်"],
    "08": ["aug", "august", "ဩဂုတ်", "သြဂုတ်"],
    "09": ["sep", "september", "စက်တင်ဘာ"],
    "10": ["oct", "october", "အောက်တိုဘာ"],
    "11": ["nov", "november", "နိုဝင်ဘာ"],
    "12": ["dec", "december", "ဒီဇင်ဘာ"],
}


# ==========================================
# ၁။ မူလ ရှိပြီးသား Functions များ (main.py အတွက် မပျက်မကွက် ထိန်းသိမ်းထားသည်)
# ==========================================

def extract_pdf_links(html_text):
    """HTML စာသားထဲမှ PDF ဒေါင်းလုဒ် Link များကို ထုတ်ယူပေးခြင်း"""
    soup = BeautifulSoup(html_text, "html.parser")
    return [
        a["href"]
        for a in soup.find_all("a", href=True)
        if "/file-download/download/public/" in a["href"]
        or "file-download" in a["href"]
        or ".pdf" in a["href"].lower()
    ]


def is_valid_pdf(content):
    """ဖိုင်သည် တကယ့် PDF အစစ်အမှန် ဟုတ်/မဟုတ် Header စစ်ဆေးခြင်း"""
    return bool(content) and content.startswith(b"%PDF-")


def absolute_url(href, base_url):
    """Relative URL ကို Full/Absolute URL သို့ ပြောင်းပေးခြင်း"""
    if not href:
        return ""
    return (
        href
        if href.startswith("http://") or href.startswith("https://")
        else base_url.rstrip("/") + "/" + href.lstrip("/")
    )


# ==========================================
# ၂။ Date Parsing & Conversion Functions (အသစ်)
# ==========================================

def to_english_digits(text: str) -> str:
    """မြန်မာ ဂဏန်းများကို အင်္ဂလိပ် ဂဏန်းသို့ ပြောင်းခြင်း"""
    mm_digits = ["၀", "၁", "၂", "၃", "၄", "၅", "၆", "၇", "၈", "၉"]
    for i, digit in enumerate(mm_digits):
        text = text.replace(digit, str(i))
    return text


def parse_universal_date(raw_text: str):
    """မည်သည့် ပုံစံဖြင့် လာသော ရက်စွဲကိုမဆို စနစ်တကျ ခွဲခြမ်းစိတ်ဖြာပေးခြင်း"""
    cleaned = to_english_digits(raw_text.strip())
    tz = pytz.timezone("Asia/Yangon")
    now = datetime.now(tz)

    # ဒီနေ့ / မနေ့က / တမြန်နေ့က
    if re.search(r"(ဒီနေ့|today|📅\s*ဒီနေ့သတင်းစာ)", cleaned, re.I) or ("ဒီနေ့" in cleaned and "သတင်းစာ" in cleaned):
        return {"year": str(now.year), "month": f"{now.month:02d}", "day": f"{now.day:02d}", "display_date": f"{now.year}-{now.month:02d}-{now.day:02d}"}
    if re.search(r"(မနေ့က|yesterday|📅\s*မနေ့ကသတင်းစာ)", cleaned, re.I) or ("မနေ့က" in cleaned and "သတင်းစာ" in cleaned):
        yest = now - timedelta(days=1)
        return {"year": str(yest.year), "month": f"{yest.month:02d}", "day": f"{yest.day:02d}", "display_date": f"{yest.year}-{yest.month:02d}-{yest.day:02d}"}
    if re.search(r"(တမြန်နေ့က)", cleaned, re.I):
        db = now - timedelta(days=2)
        return {"year": str(db.year), "month": f"{db.month:02d}", "day": f"{db.day:02d}", "display_date": f"{db.year}-{db.month:02d}-{db.day:02d}"}

    # YYYY-MM-DD
    m = re.search(r"\b(19\d{2}|20\d{2})[-/. ](\d{1,2})[-/. ](\d{1,2})\b", cleaned)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"year": y, "month": f"{mo:02d}", "day": f"{d:02d}", "display_date": f"{y}-{mo:02d}-{d:02d}"}

    # DD-MM-YYYY
    m = re.search(r"\b(\d{1,2})[-/. ](\d{1,2})[-/. ](19\d{2}|20\d{2})\b", cleaned)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"year": y, "month": f"{mo:02d}", "day": f"{d:02d}", "display_date": f"{y}-{mo:02d}-{d:02d}"}

    # မြန်မာလို ရေးသားချက် (ဥပမာ ၂၀၂၆ ဩဂုတ် ၁၈ ရက် သို့မဟုတ် ၂၀၂၃ ခုနှစ် ၆ လပိုင်း ၂၃ ရက်)
    ym = re.search(r"(19\d{2}|20\d{2})\s*(?:ခုနှစ်|ခု|နှစ်)?", cleaned)
    mm_num = re.search(r"(\d{1,2})\s*(?:လပိုင်း|လ)", cleaned)
    dm_num = re.search(r"(\d{1,2})\s*(?:ရက်နေ့|ရက်)", cleaned)

    if ym and mm_num and dm_num:
        y, mo, d = ym.group(1), int(mm_num.group(1)), int(dm_num.group(1))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return {"year": y, "month": f"{mo:02d}", "day": f"{d:02d}", "display_date": f"{y}-{mo:02d}-{d:02d}"}

    if ym and dm_num:
        cleaned_l = cleaned.lower()
        for m_code, variants in MONTH_MAP.items():
            if any(v in cleaned_l for v in variants if not v.isdigit()):
                y, d = ym.group(1), int(dm_num.group(1))
                if 1 <= d <= 31:
                    return {"year": y, "month": m_code, "day": f"{d:02d}", "display_date": f"{y}-{m_code}-{d:02d}"}

    return None


# ==========================================
# ၃။ Online Paper Scraping & Download Functions (အသစ်)
# ==========================================

def find_moi_paper_universal(prefix, base_url, day, month, year):
    """MOI Website မှ သတ်မှတ်ရက်စွဲအတွက် PDF Link ရှာဖွေခြင်း"""
    int_day = str(int(day))
    pad_day = f"{int(day):02d}"
    short_year = year[-2:]
    full_year = year

    m_variants = [v for v in MONTH_MAP.get(month, []) if not v.isdigit() and len(v) >= 3]
    slugs = []
    for mv in m_variants:
        slugs.extend([
            f"{int_day}-{mv}-{short_year}",
            f"{int_day}-{mv}-{full_year}",
            f"{pad_day}-{mv}-{short_year}",
            f"{pad_day}-{mv}-{full_year}",
            f"{int_day}_{mv}_{full_year}",
        ])
    slugs = list(dict.fromkeys(slugs))

    # ၁။ Direct URL Slugs ဖြင့် ရှာဖွေခြင်း
    for slug in slugs:
        article_url = f"{base_url.rstrip('/')}/{prefix}/{slug}"
        try:
            r = requests.get(article_url, headers=HEADERS, timeout=4, verify=False)
            if r.status_code == 200:
                links = extract_pdf_links(r.text)
                if links:
                    return absolute_url(links[0], base_url)
        except Exception:
            continue

    # ၂။ Homepage Category စာမျက်နှာမှ ရှာဖွေခြင်း
    try:
        r = requests.get(f"{base_url.rstrip('/')}/{prefix}/", headers=HEADERS, timeout=5, verify=False)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(s in href for s in slugs):
                    detail_url = absolute_url(href, base_url)
                    dr = requests.get(detail_url, headers=HEADERS, timeout=4, verify=False)
                    if dr.status_code == 200:
                        links = extract_pdf_links(dr.text)
                        if links:
                            return absolute_url(links[0], base_url)
    except Exception:
        pass

    return None


def get_mdn_backup_papers(day, month, year):
    """MOI တွင် မတွေ့ပါက MDN Backup ဆာဗာမှ သတင်းစာများ ရှာဖွေခြင်း"""
    date_formats = [
        f"{day}/{month}/{year}",
        f"{int(day)}/{int(month)}/{year}",
        f"{year}-{month}-{day}",
        f"{day}-{month}-{year}",
    ]
    for fmt in date_formats:
        try:
            r = requests.get(
                "https://www.mdn.gov.mm/newspaper/public/",
                params={"published_date": fmt},
                headers=HEADERS,
                timeout=5,
                verify=False,
            )
            if r.status_code == 200:
                ids = list(dict.fromkeys(re.findall(r"ebooks/(?:download|read)/(\d+)", r.text)))
                if ids:
                    names = ["မြန်မာ့အလင်း", "ကြေးမုံ", "The Global New Light of Myanmar"]
                    prefixes = ["myanmaalinn", "themirror", "newlightmyanmar"]
                    return [
                        {
                            "name": names[i] if i < len(names) else f"သတင်းစာ-{i+1}",
                            "file_prefix": prefixes[i] if i < len(prefixes) else f"paper_{i+1}",
                            "url": f"https://www.mdn.gov.mm/newspaper/public/ebooks/download/{pid}",
                        }
                        for i, pid in enumerate(ids)
                    ]
        except Exception:
            continue
    return []


def download_pdf_to_disk(url, local_path):
    """PDF ဖိုင်ကို Stream ဖြင့် ဒေါင်းလုဒ်ဆွဲပြီး Header စစ်ဆေးခြင်း"""
    try:
        with requests.get(url, headers=HEADERS, stream=True, timeout=(20, 180), verify=False) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        return os.path.exists(local_path) and os.path.getsize(local_path) > 5000
    except Exception as e:
        print(f"❌ Download error: {e}")
        return False
