import os
import re
import json
import time
import html
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# Використовуємо curl_cffi ТІЛЬКИ для парсингу сайтів, щоб обходити захист Cloudflare.
from curl_cffi import requests as curl_requests
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHANNEL_ID = (os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID") or "").strip()
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
ANTHROPIC_WORKSPACE_ID = (os.environ.get("ANTHROPIC_WORKSPACE_ID") or "").strip()
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 50
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 20
ARTICLE_MAX_CHARS = 4000

ANTHROPIC_TIMEOUT = 45
ANTHROPIC_MAX_RETRIES = 2
ANTHROPIC_RETRY_DELAY = 2
ANTHROPIC_CALL_DELAY = 0.2

FEEDS = [
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", True),
    ("https://ec.europa.eu/commission/presscorner/api/rss?language=en", "Єврокомісія", True),
    ("https://www.europarl.europa.eu/rss/doc/top-stories/en.xml", "Європарламент", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=foreign-commonwealth-development-office", "FCDO (Британія)", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=prime-ministers-office-10-downing-street", "Даунінг-стріт", True),
    ("https://www.gov.pl/feed/rss/diplomacy", "МЗС Польщі", True),
    ("https://www.esteri.it/en/feed/", "МЗС Італії", True),
]

ANTHROPIC_MODELS = [
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-sonnet-latest",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307"
]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MAX_TOKENS = 1024

GOOGLE_NEWS_SITE_SOURCES = [
    ("nato.int", "НАТО (Google News)"),
    ("eeas.europa.eu", "EEAS (Google News)"),
    ("bundesregierung.de", "Уряд Німеччини (Google News)"),
    ("bundestag.de", "Бундестаг (Google News)"),
    ("diplomatie.gouv.fr", "МЗС Франції (Google News)"),
    ("mfa.gov.ua", "МЗС України (Google News)"),
    ("president.gov.ua", "Офіс Президента України (Google News)"),
    ("state.gov", "Держдеп США (Google News)"),
    ("whitehouse.gov", "Білий дім (Google News)"),
    ("defense.gov", "Пентагон (Google News)"),
    ("osce.org", "ОБСЄ (Google News)"),
    ("imf.org", "МВФ (Google News)"),
    ("worldbank.org", "Світовий банк (Google News)"),
    ("press.un.org", "ООН (Google News)"),
]

for _domain, _label in GOOGLE_NEWS_SITE_SOURCES:
    FEEDS.append((
        f"https://news.google.com/rss/search?q=site:{_domain}&hl=en-US&gl=US&ceid=US:en",
        _label,
        True,
    ))

def notify_admin(message):
    if not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = f"⚠️ <b>Помилка Diplomacy Bot:</b>\n\n<pre>{html.escape(message[:3500])}</pre>"
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Не вдалося відправити помилку адміну: {e}")

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'</?[a-zA-Z0-9]+>', '', text)
    return text.strip()

def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()

def fetch_article_text(url):
    try:
        resp = curl_requests.get(url, timeout=ARTICLE_FETCH_TIMEOUT, impersonate="chrome120")
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        article = soup.find("article") or soup.find(class_=re.compile(r"(article|post|entry)[-_]?(content|body)", re.I))
        container = article if article else soup
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        text = " ".join(p for p in paragraphs if len(p) > 40)
        return text[:ARTICLE_MAX_CHARS]
    except Exception as e:
        print(f"Не вдалося завантажити текст статті ({url}): {e}")
        return ""

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}
    else:
        data = {}
    if isinstance(data, list):
        data = {"links": data, "recent_posts": []}
    data.setdefault("links", [])
    data.setdefault("recent_posts", [])
    return data

def save_history(history):
    history["links"] = list(dict.fromkeys(history["links"]))[-1200:]
    history["recent_posts"] = history["recent_posts"][-RECENT_POSTS_FOR_DEDUP:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def analyze_with_claude(title, article_text, source_name, recent_posts):
    if not ANTHROPIC_API_KEY:
        return {"relevant": True, "duplicate": False, "title": None, "analysis": None, "failed": False}

    recent_block = "(поки що порожньо — це перша перевірка)"
    if recent_posts:
        recent_block = "\n".join(
            f"- [{p['source']}] {p['title']}: {p['summary']}" for p in recent_posts
        )

    prompt = (
        "Ти — старший геополітичний аналітик
