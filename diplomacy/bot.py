import os
import re
import json
import time
import html
import traceback
from datetime import datetime, timezone

# Використовуємо curl_cffi замість звичайного requests для обходу Cloudflare
from curl_cffi import requests
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID")

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 40
RECENT_POSTS_FOR_DEDUP = 15  
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 20
ARTICLE_MAX_CHARS = 4000

# Оновлені джерела з працюючими RSS + Google Alerts
FEEDS = [
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Council of the EU", True),
    ("https://ec.europa.eu/commission/presscorner/api/rss", "European Commission", True),
    ("https://eeas.europa.eu/topics/sanctions-policy/rss_en", "EEAS", True),
    ("https://press.un.org/en/rss.xml", "UN Press", True),
    ("https://news.un.org/feed/subscribe/en/news/all/rss.xml", "UN News", True),
    ("https://www.nato.int/cps/en/natohq/news.xml", "NATO", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=foreign-commonwealth-development-office", "UK FCDO", True),
    ("https://www.state.gov/press-releases/feed/", "U.S. Department of State", True),
    ("https://www.whitehouse.gov/briefing-room/feed/", "The White House", True),
    ("https://www.diplomatie.gouv.fr/spip.php?page=backend&id_rubrique=260", "France Diplomacy", True),
    
    # --- Розширений пошук дипломатичних інсайдів (кастомні назви для Telegram) ---
    ("https://www.google.com/alerts/feeds/12089626364797798521/39472708417579504", "Euro-Atlantic Security", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/2190225341532693885", "Diplomatic Summits", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/11336788638066702601", "Global Diplomacy", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/2277749747477857648", "Western Policy & Pacts", True),
]

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def notify_admin(message):
    if not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = f"⚠️ <b>Помилка Diplomacy Bot:</b>\n\n<pre>{html.escape(message[:3500])}</pre>"
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Не вдалося відправити помилку адміну: {e}")


def clean_text(text: str) -> str:
    """
    Розкодовує HTML-сутності (наприклад &quot; на ") та видаляє внутрішні теги від пошуковиків.
    """
    if not text:
        return ""
    # Спочатку декодуємо всі HTML сутності
    text = html.unescape(text)
    # Потім видаляємо всі HTML теги (наприклад <b>, <i>, тощо), щоб вони не зламали форматування
    text = re.sub(r'</?[a-zA-Z0-9]+>', '', text)
    return text.strip()


def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()


def fetch_article_text(url):
    try:
        resp = requests.get(
            url,
            timeout=ARTICLE_FETCH_TIMEOUT,
            impersonate="chrome120"
        )
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
    history["links"] = history["links"][-800:]
    history["recent_posts"] = history["recent_posts"][-RECENT_POSTS_FOR_DEDUP:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def analyze_with_groq(title, article_text, source_name, recent_posts):
    if not GROQ_API_KEY:
        return {"relevant": True, "duplicate": False, "title": None, "analysis": None}

    if recent_posts:
        recent_block = "\n".join(
            f"- [{p['source']}] {p['title']}: {p['summary']}" for p in recent_posts
        )
    else:
        recent_block = "(поки що порожньо — це перша перевірка)"

    prompt = (
        "Ти — редактор дипломатичних новин для українського Telegram-каналу. "
        "Твоя мета: висвітлювати важливі міжнародні зустрічі, візити, саміти та підписання угод "
        "між офіційними особами.\n\n"
        "ГЕОГРАФІЧНИЙ ФІЛЬТР (ДУЖЕ ВАЖЛИВО): Наш канал фокусується на Євро-Атлантичному геополітичному просторі "
        "(США, Велика Британія, Європа, НАТО, ЄС, Україна). Ти МАЄШ СУВОРО ІГНОРУВАТИ будь-які регіональні події, "
        "саміти та візити, які стосуються виключно країн Азії, Африки, Близького Сходу чи Південної Америки "
        "(наприклад, саміт Африканського Союзу або візит міністра Індії до Китаю), ЯКЩО в них не беруть активної участі лідери США, Європи або Західних організацій.\n\n"
        f"Джерело цієї новини: {source_name}.\n"
        f"Оригінальний заголовок: {title}\n\n"
        f"Текст новини:\n{article_text}\n\n"
        "ОСТАННІ ОПУБЛІКОВАНІ В КАНАЛІ ПОСТИ (для перевірки на повтор):\n"
        f"{recent_block}\n\n"
        "Виконай ПОСЛІДОВНО:\n\n"
        "Крок 1 (relevant): чи ця новина ДІЙСНО про дипломатичну подію/зустріч/саміт, І чи проходить вона "
        "наш ГЕОГРАФІЧНИЙ ФІЛЬТР (стосується Заходу/України)? Якщо це локальна подія суто між країнами Азії/Африки, "
        "або просто загальна заява чи внутрішня політика — одразу поверни "
        '{"relevant": false, "duplicate": false, "title": null, "analysis": null} і більше нічого.\n\n'
        "Крок 2 (duplicate, лише якщо relevant=true): чи описує ця новина ТУ САМУ подію (ту саму "
        "зустріч/саміт), що вже є в списку останніх опублікованих постів вище? Якщо так — поверни "
        '{"relevant": true, "duplicate": true, "title": null, "analysis": null} і більше нічого.\n\n'
        "Крок 3 (лише якщо relevant=true і duplicate=false):\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів).\n"
        "2) Дай 2-3 речення дипломатичного коментаря українською на основі фактів зі статті.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"relevant": true, "duplicate": false, "title": "...", "analysis": "..."}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            msg = f"Groq: HTTP {resp.status_code}: {resp.text[:500]}"
            print(msg)
            notify_admin(msg)
            return {"relevant": True, "duplicate": False, "title": None, "analysis": None}
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        text = text.replace("```json", "").replace("
