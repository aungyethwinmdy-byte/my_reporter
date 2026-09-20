"""
================================================================
INDEPENDENT MEDIA INCREMENTAL INGESTION PIPELINE (Unified Hybrid)
================================================================
File Name: fetch_independent_news.py
Strategy:
- BBC Burmese     : Direct RSS Feed & Full-text Extractor
- The Irrawaddy   : Official Telegram Mirror (Bypasses Cloudflare 403)
- RFA Burmese     : Official Telegram Mirror (Bypasses Cloudflare/Timeout)
Deduplication:
- Supabase URL Upsert (on_conflict='url') — the URL column is the ONLY dedup
  key. `content_hash` is stored for reference but is NOT a conflict target, so
  the same article republished under a different URL is stored twice.
- Note the t.me fallback below: when a Telegram post carries no outbound
  article link, the *message* URL is stored instead. Those message URLs are
  unique per post, so a repost of identical content produces a second row with
  an identical content_hash. Making content_hash the conflict target would
  close that gap, but it requires a matching UNIQUE constraint in the table.
================================================================
"""

from __future__ import annotations

import os
import re
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional

import requests
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from env_config import get_supabase_credentials
from supabase import create_client, Client

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=env_path)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("NewsIngestor")

SUPABASE_URL, SUPABASE_KEY = get_supabase_credentials()
INDEPENDENT_TABLE_NAME = os.getenv("INDEPENDENT_TABLE_NAME", "independent_articles")

# NOTE: the client is created lazily (see get_supabase_client) so that importing
# this module — unit tests, tooling, CI — never raises on missing credentials.
_supabase_client: Optional[Client] = None


def get_supabase_client() -> Optional[Client]:
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        try:
            _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as exc:
            logger.error("❌ Supabase init failed: %s", exc)
            return None
    return _supabase_client

MYANMAR_TZ = timezone(timedelta(hours=6, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "my-MM,my;q=0.9,en-US;q=0.8,en;q=0.7",
}

PROXIES = {
    "http": os.environ.get("HTTP_PROXY", None),
    "https": os.environ.get("HTTPS_PROXY", None),
}

# --------------------------------------------------------------
# Source Configuration
# --------------------------------------------------------------

# Direct RSS ဖတ်၍ရသော Sources
RSS_FEEDS = {
    "BBC Burmese": "https://feeds.bbci.co.uk/burmese/rss.xml",
}

# Cloudflare ခံထား၍ Telegram Channel Mirror မှ ဖတ်မည့် Sources
TELEGRAM_SOURCES = {
    "The Irrawaddy": [
        "https://t.me/s/theirrawaddy",
        "https://telegram.me/s/theirrawaddy",
    ],
    "RFA Burmese": [
        "https://t.me/s/RFA_Burmese",
        "https://t.me/s/rfaburmese",
    ],
}


def clean_html_text(raw_html: str) -> str:
    """HTML tag များနှင့် spacing များကို သန့်စင်ခြင်း"""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        element.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def calculate_hash(text: str) -> str:
    """စာသားအပြည့်အစုံကို SHA-256 hash ပြုလုပ်ခြင်း"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_published_date(entry: dict) -> str:
    """RSS published date မှ YYYY-MM-DD ပြောင်းလဲခြင်း"""
    if "published" in entry:
        try:
            dt = parsedate_to_datetime(entry["published"])
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.now(MYANMAR_TZ).strftime("%Y-%m-%d")


def extract_full_article_body(url: str, timeout: int = 10) -> str:
    """Web စာမျက်နှာမှ စာကိုယ် အပြည့်အစုံ ဆွဲယူခြင်း (BBC အတွက်သာ)"""
    try:
        resp = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=timeout)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.content, "html.parser")
        paragraphs = soup.find_all("p")
        body_text = " ".join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 20])
        return re.sub(r"\s+", " ", body_text).strip()[:10000]
    except Exception:
        return ""


# --------------------------------------------------------------
# Ingestion Logic: RSS (BBC)
# --------------------------------------------------------------

def fetch_from_rss(source_name: str, feed_url: str) -> List[Dict]:
    logger.info("Collecting source: %s (RSS Feed)", source_name)
    collected = []
    try:
        resp = requests.get(feed_url, headers=HEADERS, proxies=PROXIES, timeout=15)
        if resp.status_code != 200:
            logger.warning("⚠️ RSS status %s for %s", resp.status_code, source_name)
            return []

        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:10]:
            url = getattr(entry, "link", "").strip()
            headline = clean_html_text(getattr(entry, "title", ""))
            pub_date = parse_published_date(entry)

            if not url or not headline:
                continue

            summary = clean_html_text(getattr(entry, "summary", ""))
            full_body = extract_full_article_body(url)
            body_text = full_body if len(full_body) > 100 else summary

            text_for_hash = f"{headline}\n{body_text}"
            content_hash = calculate_hash(text_for_hash)

            collected.append({
                "source_name": source_name,
                "published_date": pub_date,
                "headline": headline,
                "body_text": body_text or headline,
                "url": url,
                "category": "News",
                "content_hash": content_hash,
            })
    except Exception as e:
        logger.error("❌ RSS error for %s: %s", source_name, e)

    return collected


# --------------------------------------------------------------
# Ingestion Logic: Telegram Mirror (The Irrawaddy & RFA)
# --------------------------------------------------------------

def fetch_from_telegram(source_name: str, mirror_urls: List[str]) -> List[Dict]:
    logger.info("Collecting source: %s (Telegram Mirror Channel)", source_name)
    resp = None
    used_url = None

    for target_url in mirror_urls:
        try:
            r = requests.get(target_url, headers=HEADERS, proxies=PROXIES, timeout=15)
            if r.status_code == 200:
                resp = r
                used_url = target_url
                break
        except Exception:
            continue

    if not resp or resp.status_code != 200:
        logger.warning("⚠️ Could not reach Telegram mirror for %s", source_name)
        return []

    collected = []
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.find_all("div", class_="tgme_widget_message")

        if not messages:
            # A t.me/s/<name> link can resolve to a user *contact* page instead of
            # a channel preview. Those return HTTP 200 with the title
            # "Telegram: Contact @<name>", ~9 KB of body and zero message blocks,
            # so they look like a successful fetch that simply had nothing new.
            # RFA Burmese was configured exactly this way (t.me/s/RFA_Burmese and
            # t.me/s/rfaburmese are both user accounts) and silently contributed
            # zero articles for the whole time it was listed as a monitored
            # source. Say so explicitly rather than returning a bare [].
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
            if "contact" in title.lower():
                logger.error(
                    "❌ %s: %s is a user contact page, not a channel — no web "
                    "preview to scrape. Point this at a public channel with a "
                    "/s/ preview URL.",
                    source_name, used_url,
                )
            else:
                logger.warning(
                    "⚠️ %s: %s returned no message blocks (channel empty, or the "
                    "preview markup changed).",
                    source_name, used_url,
                )
            return []

        domain_pattern = re.compile(r"(irrawaddy\.com|rfa\.org)", re.IGNORECASE)

        # နောက်ဆုံးတင်ထားသော သတင်း ၁၅ ပုဒ်ကို ယူခြင်း
        for msg in messages[-15:]:
            text_block = msg.find("div", class_="tgme_widget_message_text")
            if not text_block:
                continue

            full_text = text_block.get_text(separator="\n").strip()
            if not full_text or len(full_text) < 20:
                continue

            # သတင်း article link ရှာခြင်း (မရှိပါက Telegram post link အား fallback သုံးသည်)
            link_tag = text_block.find("a", href=domain_pattern)
            article_url = link_tag.get("href") if link_tag else None

            if not article_url:
                # Fallback: store the Telegram message URL itself. This keeps the
                # content (useful for /compare) but means the `url` column is not
                # always an article link — in production ~55% of rows are t.me
                # links. Because url is the only dedup key, a repost of the same
                # content under a new message URL inserts a second row.
                msg_link_tag = msg.find("a", class_="tgme_widget_message_date")
                article_url = msg_link_tag.get("href") if msg_link_tag else None

            if not article_url:
                continue

            # Message post လုပ်သည့် အချိန်ရယူခြင်း
            pub_date = datetime.now(MYANMAR_TZ).strftime("%Y-%m-%d")
            time_tag = msg.find("time")
            if time_tag and time_tag.get("datetime"):
                try:
                    dt = datetime.fromisoformat(time_tag["datetime"])
                    pub_date = dt.astimezone(MYANMAR_TZ).strftime("%Y-%m-%d")
                except Exception:
                    pass

            lines = [line.strip() for line in full_text.splitlines() if line.strip()]
            headline = lines[0] if lines else f"{source_name} Update"
            body_text = "\n".join(lines[1:]) if len(lines) > 1 else full_text

            if len(headline) < 8:
                continue

            content_hash = calculate_hash(full_text)

            collected.append({
                "source_name": source_name,
                "published_date": pub_date,
                "headline": clean_html_text(headline)[:500],
                "body_text": clean_html_text(body_text) or clean_html_text(headline),
                "url": article_url.strip(),
                "category": "News",
                "content_hash": content_hash,
            })
    except Exception as e:
        logger.exception("❌ Telegram mirror parse error for %s: %s", source_name, e)

    return collected


# --------------------------------------------------------------
# Database Sync (Batch Upsert)
# --------------------------------------------------------------

def save_articles_to_supabase(articles: List[Dict]) -> int:
    """Supabase independent_articles table ထဲသို့ Batch Upsert ဖြင့် သိမ်းဆည်းခြင်း"""
    if not articles:
        return 0

    client = get_supabase_client()
    if not client:
        logger.error("❌ Supabase credentials မပြည့်စုံပါ။ Sync ကျော်လိုက်ပါသည်။")
        return 0

    try:
        response = client.table(INDEPENDENT_TABLE_NAME).upsert(
            articles,
            on_conflict="url",
            ignore_duplicates=True
        ).execute()

        # With ignore_duplicates=True PostgREST returns only the rows that were
        # actually inserted, so an empty response means "every candidate was
        # already present" — a perfectly normal, healthy run. The old
        # `len(response.data) if response.data else len(articles)` treated that
        # empty list as a failure to read and fell back to the candidate count,
        # so a run that inserted nothing reported "N articles processed".
        inserted = len(response.data) if response.data else 0
        logger.info(
            "✅ Supabase Batch Synced: %s new, %s already present (candidates: %s).",
            inserted, len(articles) - inserted, len(articles),
        )
        return inserted
    except Exception as e:
        logger.error("❌ Supabase sync failed: %s", e)
        return 0


def fetch_all() -> int:
    today_str = datetime.now(MYANMAR_TZ).strftime("%Y-%m-%d")
    logger.info("=" * 60)
    logger.info("🚀 Unified News Ingestion Run Started | %s", today_str)
    logger.info("=" * 60)

    all_articles = []
    per_source: Dict[str, int] = {}

    # ၁။ BBC Burmese (Direct RSS)
    for source_name, feed_url in RSS_FEEDS.items():
        got = fetch_from_rss(source_name, feed_url)
        per_source[source_name] = len(got)
        all_articles.extend(got)

    # ၂။ The Irrawaddy & RFA Burmese (Telegram Channel Mirror)
    for source_name, mirror_urls in TELEGRAM_SOURCES.items():
        got = fetch_from_telegram(source_name, mirror_urls)
        per_source[source_name] = len(got)
        all_articles.extend(got)

    logger.info("📊 Total candidate articles collected: %s", len(all_articles))
    for name, n in per_source.items():
        logger.info("   • %-16s %s", name, n)

    # A source that yields nothing is a silent data gap: it still appears in
    # /sources as "monitored" and in MONITORED_SOURCES, so the bot claims
    # coverage it does not have. RFA Burmese sat at 0 for exactly this reason —
    # its configured t.me URLs resolve to a *contact* page ("Telegram: Contact
    # @RFA_Burmese"), not a channel preview, so there are no message blocks to
    # parse. Surface it loudly instead of letting the run look healthy.
    dead = [name for name, n in per_source.items() if n == 0]
    if dead:
        logger.warning(
            "⚠️ Source(s) returned ZERO articles: %s — check the configured feed/"
            "mirror URLs; a t.me link that resolves to a 'Contact @name' page is "
            "a user account, not a channel, and will never yield messages.",
            ", ".join(dead),
        )

    synced_count = save_articles_to_supabase(all_articles)

    logger.info("=" * 60)
    logger.info("🏁 Pipeline Finished. Total articles synced: %s", synced_count)
    logger.info("=" * 60)
    return synced_count


if __name__ == "__main__":
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("❌ Supabase Credentials မပြည့်စုံပါ။ .env ဖိုင်ကို စစ်ဆေးပါ။")

    fetch_all()