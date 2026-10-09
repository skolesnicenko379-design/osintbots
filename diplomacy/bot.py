import os
import re
import json
import time
import html
import traceback
import urllib.parse
from datetime import datetime, timezone, timedelta

import feedparser
from bs4 import BeautifulSoup
import requests

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip().replace('"', '').replace("'", "")
CHANNEL_ID = (os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip().replace('"', '').replace("'", "")
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 10
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 15
ARTICLE_MAX_CHARS = 4000
GROQ_TIMEOUT = 45
GROQ_MAX_RETRIES = 0
GROQ_RETRY_DELAY = 4
GROQ_CALL_DELAY = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# --- ДЖЕРЕЛА ДИПЛОМАТИЧНОГО БОТА ---
search_queries = [
    ("US foreign policy OR diplomacy OR State Department when:24h", "US Diplomacy"),
    ("US sanctions OR trade tariffs OR export controls China when:24h", "Geo-Economics"),
    ("Pentagon OR defense budget OR military aid Ukraine Taiwan when:24h", "Global Defense"),
    ("geopolitics alliance NATO Indo-Pacific US when:24h", "Strategic Alliances"),
    ("European Union foreign policy OR EEAS geopolitics when:24h", "EU Diplomacy")
]

FEEDS = []
for query, label in search_queries:
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    FEEDS.append((url, label, True))

FEEDS.extend([
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", True),
    ("https://www.europarl.europa.eu/rss/doc/top-stories/en.xml", "Європарламент", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=foreign-commonwealth-development-office", "FCDO (Британія)", True),
])

# АКТУАЛЬНІ РОБОЧІ МОДЕЛІ GROQ
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it"
]
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
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r'</?[a-zA-Z0-9]+>', '', text)
    return text.strip()


def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()


def fetch_article_text(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=ARTICLE_FETCH_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()

        article = soup.find("article") or soup.find(class_=re.compile(r"(article|post|entry)[-_]?(content|body)", re.I))
        container = article if article else soup
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        text = " ".join(p for p in paragraphs if len(p) > 40)
        return text[:ARTICLE_MAX_CHARS]
    except Exception:
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
        
    if not isinstance(data, dict):
        data = {}

    data.setdefault("links", [])
    data.setdefault("recent_posts", [])
    return data


def save_history(history):
    history["links"] = list(dict.fromkeys(history["links"]))[-1200:]
    history["recent_posts"] = history["recent_posts"][-RECENT_POSTS_FOR_DEDUP:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _is_model_dead_error(status_code, resp_text):
    if status_code == 404:
        return True
    if status_code == 400 and ("model_decommissioned" in (resp_text or "") or "does not exist" in (resp_text or "")):
        return True
    return False


def analyze_with_groq(title, article_text, source_name, recent_posts):
    if not GROQ_API_KEY:
        print("КРИТИЧНА ПОМИЛКА: GROQ_API_KEY не знайдено!")
        return {"relevant": False, "duplicate": False, "title": None, "analysis": None, "failed": True}

    recent_block = "(поки що порожньо — це перша перевірка)"
    if recent_posts:
        recent_block = "\n".join(
            f"- [{p['source']}] {p['title']}: {p['summary']}" for p in recent_posts
        )

    prompt = (
        "Ти — аналітик трансатлантичного геополітичного каналу. "
        "Твоя мета: аналізувати ключові міжнародні події, саміти, рішення НАТО/ЄС.\n\n"
        f"Джерело: {source_name}\n"
        f"Заголовок: {title}\n\n"
        f"Текст статті:\n{article_text}\n\n"
        "ОСТАННІ ПОСТИ (перевірка на дублікати):\n"
        f"{recent_block}\n\n"
        "Завдання:\n"
        "1. Якщо подія нерелевантна (рутина/дрібниці) — поверни relevant: false.\n"
        "2. Якщо це дубль події з останніх постів — поверни duplicate: true.\n"
        "3. Якщо все добре:\n"
        "- Створи лаконічний заголовок українською (до 14 слів).\n"
        "- Напиши глибокий аналітичний коментар (2–3 речення) про значення події.\n"
        "КРИТИЧНО: Поле 'analysis' НІКОЛИ не повинно бути порожнім.\n\n"
        "Відповідай ТІЛЬКИ чистим JSON:\n"
        '{"relevant": true, "duplicate": false, "title": "Твій заголовок", "analysis": "Твій аналіз"}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None

    for model in GROQ_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        for attempt in range(1, GROQ_MAX_RETRIES + 2):
            time.sleep(GROQ_CALL_DELAY)
            try:
                resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)

                if _is_model_dead_error(resp.status_code, resp.text):
                    last_error = f"Groq: модель '{model}' недоступна: {resp.text[:300]}"
                    print(last_error)
                    break

                if resp.status_code == 429:
                    retry_after = GROQ_RETRY_DELAY
                    try:
                        retry_after = float(resp.headers.get("retry-after", GROQ_RETRY_DELAY))
                    except Exception:
                        pass
                    last_error = f"Groq 429 (rate limit) для моделі {model}, спроба {attempt}, чекаю {retry_after}с"
                    print(last_error)
                    time.sleep(retry_after)
                    continue

                if resp.status_code != 200:
                    last_error = f"Groq ({model}): HTTP {resp.status_code}: {resp.text[:500]}"
                    print(last_error)
                    time.sleep(GROQ_RETRY_DELAY)
                    continue

                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                parsed = json.loads(text)

                ai_title = parsed.get("title")
                ai_analysis = parsed.get("analysis")

                if not ai_analysis or len(ai_analysis.strip()) < 10:
                    ai_analysis = "Деталі події наразі опрацьовуються експертною групою. Очікуйте подальшої аналітики."

                return {
                    "relevant": bool(parsed.get("relevant", True)),
                    "duplicate": bool(parsed.get("duplicate", False)),
                    "title": ai_title if ai_title else title,
                    "analysis": ai_analysis,
                    "failed": False,
                }
            except Exception as e:
                last_error = f"Groq ({model}): помилка обробки (спроба {attempt}): {e}"
                print(last_error)
                time.sleep(GROQ_RETRY_DELAY)
                continue

    notify_admin(f"Groq не відповів для статті «{title}» (усі моделі вичерпано).\n{last_error}")
    return {"relevant": False, "duplicate": False, "title": None, "analysis": None, "failed": True}


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code == 429:
            retry_after = response.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(retry_after)
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        return response.status_code == 200
    except Exception as e:
        print(f"Помилка запиту до Telegram: {e}")
        return False


def collect_entries():
    all_entries = []
    now_utc = datetime.now(timezone.utc)
    max_age_delta = timedelta(hours=MAX_ARTICLE_AGE_HOURS)

    for feed_url, source_name, needs_translation in FEEDS:
        try:
            time.sleep(2)
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"Не вдалося завантажити фід {source_name}: {e}")
            continue

        if getattr(feed, "bozo", False) and not feed.entries:
            continue

        for entry in feed.entries[:10]:
            link = entry.get("link")
            if not link:
                continue

            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published_struct:
                continue

            published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)

            if (now_utc - published_dt) > max_age_delta:
                continue

            all_entries.append({
                "link": link.strip(),
                "title": entry.get("title", "Без заголовка"),
                "summary": strip_html(entry.get("summary", ""))[:1500],
                "source": source_name,
                "published": published_dt,
            })

    all_entries.sort(key=lambda e: e["published"], reverse=True)
    return all_entries


def format_message(entry, ai_title, ai_analysis):
    raw_title = ai_title or entry["title"]
    clean_title = clean_text(raw_title)
    safe_title = html.escape(clean_title)

    date_str = entry["published"].strftime("%d.%m.%Y")
    safe_source = html.escape(entry['source'])

    parts = [
        f"<b>{safe_title}</b>",
        "",
        f"🗓 {date_str} | 🏛 {safe_source}",
    ]

    if ai_analysis and len(ai_analysis.strip()) > 5:
        clean_analysis = clean_text(ai_analysis)
        safe_analysis = html.escape(clean_analysis)
        parts += ["", f"🤝 {safe_analysis}"]

    safe_link = entry['link'].replace('"', '%22')
    parts += ["", f'<a href="{safe_link}">Читати першоджерело</a>']
    return "\n".join(parts)


def main():
    history = load_history()
    entries = collect_entries()
    print(f"Зібрано свіжих записів з усіх {len(FEEDS)} фідів: {len(entries)}")

    new_posts = 0
    checked = 0

    for entry in entries:
        if new_posts >= MAX_POSTS_PER_RUN or checked >= MAX_ENTRIES_CHECKED_PER_RUN:
            break

        if entry["link"] in history["links"]:
            continue

        checked += 1

        article_text = fetch_article_text(entry["link"])
        if not article_text:
            article_text = entry["summary"]

        result = analyze_with_groq(
            entry["title"], article_text, entry["source"], history["recent_posts"]
        )

        if result.get("failed"):
            print(f"Пропущено тимчасово (Groq не відповів): {entry['title']}")
            continue

        if not result["relevant"]:
            history["links"].append(entry["link"])
            print(f"Пропущено (відхилено ШІ як нерелевантне): {entry['title']}")
            continue

        if result["duplicate"]:
            history["links"].append(entry["link"])
            print(f"Пропущено (дублює вже опубліковану подію): {entry['title']}")
            continue

        message = format_message(entry, result["title"], result["analysis"])

        if send_to_telegram(message):
            history["links"].append(entry["link"])
            history["recent_posts"].append({
                "title": result["title"] or entry["title"],
                "summary": (result["analysis"] or entry["summary"])[:300],
                "source": entry["source"],
            })
            new_posts += 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(3)
        else:
            print(f"Не вдалося опублікувати: {entry['title']}")

    save_history(history)
    print(f"Готово. Перевірено свіжих: {checked}, опубліковано: {new_posts}")


if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        try:
            main()
        except Exception as e:
            error_trace = traceback.format_exc()
            print(f"Критична помилка виконання:\n{error_trace}")
            notify_admin(f"Критичне падіння скрипта:\n{error_trace}")
    else:
        print("Помилка: TELEGRAM_TOKEN або CHANNEL_ID не задано.")
