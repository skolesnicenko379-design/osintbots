import os
import json
import time
import html
from datetime import datetime, timezone

import re
import requests
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # необов'язково — без нього бот працює в простому режимі

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 4
REQUEST_TIMEOUT = 20

# Джерела: (URL, Назва джерела, чи потрібен переклад)
FEEDS = [
    ("https://mil.in.ua/uk/news/feed/", "mil.in.ua", False),
    ("https://defence-ua.com/rss.xml", "defence-ua.com", False),
    ("https://breakingdefense.com/feed/", "Breaking Defense", True),
]

# УВАГА: на відміну від Gemini/Grok, у Groq немає псевдоніма типу "-latest",
# який сам перемикається на нову модель. Groq періодично знімає моделі з
# безкоштовного тарифу або ретайрить їх — якщо бот почне падати з 404/400 на
# цю модель, зайдіть на https://console.groq.com/docs/models, подивіться
# актуальний безкоштовний список і поміняйте значення нижче вручну.
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

ARTICLE_FETCH_TIMEOUT = 15
ARTICLE_MAX_CHARS = 4000  # скільки символів тексту статті передавати в Groq


def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()


def fetch_article_text(url):
    """Намагається витягти повний текст статті зі сторінки. Повертає '' при невдачі."""
    try:
        resp = requests.get(
            url,
            timeout=ARTICLE_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MilTechBot/1.0)"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Прибираємо явно нерелевантні блоки
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()

        # Шукаємо основний контейнер статті, якщо є — інакше беремо всі <p>
        article = soup.find("article") or soup.find(class_=re.compile(r"(article|post|entry)[-_]?(content|body)", re.I))
        container = article if article else soup
        paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        text = " ".join(p for p in paragraphs if len(p) > 40)
        return text[:ARTICLE_MAX_CHARS]
    except Exception as e:
        print(f"Не вдалося завантажити текст статті ({url}): {e}")
        return ""


# ===== Історія публікацій =====
def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []


def save_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-300:], f, ensure_ascii=False, indent=2)


# ===== Groq: переклад + коротка технічна аналітика =====
def enrich_with_groq(title, article_text, source_name, needs_translation):
    """Повертає (заголовок_укр, короткий_аналітичний_коментар) або (None, None) при помилці.

    article_text — це повний (або майже повний) текст новини, а не просто заголовок:
    аналітика будується саме на змісті статті.
    """
    if not GROQ_API_KEY:
        return None, None

    lang_note = (
        "Оригінал англійською — переклади заголовок природною українською."
        if needs_translation
        else "Оригінал вже українською."
    )

    prompt = (
        "Ти — редактор мілтех-новин для українського Telegram-каналу.\n"
        f"Джерело: {source_name}. {lang_note}\n\n"
        f"Оригінальний заголовок: {title}\n\n"
        f"Повний текст новини:\n{article_text}\n\n"
        "Виконай ДВІ речі на основі ЗМІСТУ новини вище (не лише заголовка):\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів), без клікбейту.\n"
        "2) Дай 2-3 речення технічного/аналітичного коментаря українською на основі фактів "
        "зі статті — що саме сталося, які характеристики техніки/озброєння згадуються, "
        "який можливий військовий чи практичний вплив. Без води і загальних фраз.\n\n"
        "Якщо текст новини порожній або занадто короткий для аналізу — постав analysis в null.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"title": "...", "analysis": "..."}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            print(f"Groq: HTTP {resp.status_code}: {resp.text[:500]}")
            return None, None
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        # На випадок, якщо модель обгорне відповідь у ```json ... ```
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return parsed.get("title"), parsed.get("analysis")
    except Exception as e:
        print(f"Groq: не вдалося обробити новину ({e})")
        return None, None


# ===== Telegram =====
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
            print(f"Telegram rate limit, чекаю {retry_after}с")
            time.sleep(retry_after)
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        return response.status_code == 200
    except requests.RequestException as e:
        print(f"Помилка запиту до Telegram: {e}")
        return False


# ===== Збір новин з усіх фідів =====
def collect_entries():
    all_entries = []
    for feed_url, source_name, needs_translation in FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"Не вдалося завантажити фід {feed_url}: {e}")
            continue

        for entry in feed.entries[:5]:
            link = entry.get("link")
            if not link:
                continue

            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if published_struct:
                published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
            else:
                published_dt = datetime.now(timezone.utc)

            all_entries.append({
                "link": link,
                "title": entry.get("title", "Без заголовка"),
                "summary": strip_html(entry.get("summary", ""))[:1500],
                "source": source_name,
                "needs_translation": needs_translation,
                "published": published_dt,
            })

    # Найсвіжіші новини — першими, незалежно з якого фіду
    all_entries.sort(key=lambda e: e["published"], reverse=True)
    return all_entries


def format_message(entry, groq_title, groq_analysis):
    title = groq_title or entry["title"]
    date_str = entry["published"].strftime("%d.%m.%Y")

    parts = [
        f"<b>{html.escape(title)}</b>",
        "",
        f"🗓 {date_str} | 📡 {html.escape(entry['source'])}",
    ]

    if groq_analysis:
        parts += ["", f"🔎 {html.escape(groq_analysis)}"]

    parts += ["", f"<a href='{html.escape(entry['link'])}'>Читати першоджерело</a>"]
    return "\n".join(parts)


def main():
    history = load_history()
    entries = collect_entries()
    new_posts = 0

    for entry in entries:
        if new_posts >= MAX_POSTS_PER_RUN:
            break
        if entry["link"] in history:
            continue

        article_text = fetch_article_text(entry["link"])
        if not article_text:
            article_text = entry["summary"]  # fallback: хоч короткий опис з RSS

        groq_title, groq_analysis = enrich_with_groq(
            entry["title"], article_text, entry["source"], entry["needs_translation"]
        )

        message = format_message(entry, groq_title, groq_analysis)

        if send_to_telegram(message):
            history.append(entry["link"])
            new_posts += 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(3)  # щоб Telegram не вважав це спамом
        else:
            print(f"Не вдалося опублікувати: {entry['title']}")

    save_history(history)
    print(f"Готово. Опубліковано новин: {new_posts}")


if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        main()
    else:
        print("Помилка: не знайдені TELEGRAM_TOKEN або CHANNEL_ID")
