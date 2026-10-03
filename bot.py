import os
import json
import time
import html
from datetime import datetime, timezone

import requests
import feedparser

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
XAI_API_KEY = os.environ.get("XAI_API_KEY")  # необов'язково — без нього бот працює в простому режимі

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 4
REQUEST_TIMEOUT = 20

# Джерела: (URL, Назва джерела, чи потрібен переклад)
FEEDS = [
    ("https://mil.in.ua/uk/news/feed/", "mil.in.ua", False),
    ("https://defence-ua.com/rss.xml", "defence-ua.com", False),
    ("https://breakingdefense.com/feed/", "Breaking Defense", True),
]

# "grok-latest" — офіційний псевдонім xAI, який сам завжди вказує на найновішу
# модель Grok. Оновлювати цей рядок вручну не потрібно.
XAI_MODEL = "grok-latest"
XAI_API_URL = "https://api.x.ai/v1/chat/completions"


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


# ===== Grok (xAI): переклад + коротка технічна аналітика =====
def enrich_with_grok(title, summary, source_name, needs_translation):
    """Повертає (заголовок_укр, короткий_аналітичний_коментар) або (None, None) при помилці."""
    if not XAI_API_KEY:
        return None, None

    lang_note = (
        "Текст оригіналу англійською — переклади природною українською."
        if needs_translation
        else "Текст оригіналу вже українською — просто онови стиль за потреби."
    )

    prompt = (
        "Ти — редактор мілтех-новин для українського Telegram-каналу.\n"
        f"Джерело: {source_name}.\n"
        f"{lang_note}\n\n"
        f"Заголовок: {title}\n"
        f"Короткий опис: {summary}\n\n"
        "Виконай ДВІ речі:\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів), без клікбейту.\n"
        "2) Дай 1-2 речення технічного/аналітичного коментаря українською — що це означає "
        "практично (тип техніки/озброєння, можливий вплив, контекст), без води.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"title": "...", "analysis": "..."}'
    )

    headers = {
        "Authorization": f"Bearer {XAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": XAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }

    try:
        resp = requests.post(XAI_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        # На випадок, якщо модель обгорне відповідь у ```json ... ```
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return parsed.get("title"), parsed.get("analysis")
    except Exception as e:
        print(f"Grok: не вдалося обробити новину ({e})")
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
                "summary": entry.get("summary", "")[:500],
                "source": source_name,
                "needs_translation": needs_translation,
                "published": published_dt,
            })

    # Найсвіжіші новини — першими, незалежно з якого фіду
    all_entries.sort(key=lambda e: e["published"], reverse=True)
    return all_entries


def format_message(entry, grok_title, grok_analysis):
    title = grok_title or entry["title"]
    date_str = entry["published"].strftime("%d.%m.%Y %H:%M") + " UTC"

    parts = [
        f"<b>{html.escape(title)}</b>",
        "",
        f"🗓 {date_str} | 📡 {html.escape(entry['source'])}",
    ]

    if grok_analysis:
        parts += ["", f"🔎 {html.escape(grok_analysis)}"]

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

        grok_title, grok_analysis = enrich_with_grok(
            entry["title"], entry["summary"], entry["source"], entry["needs_translation"]
        )

        message = format_message(entry, grok_title, grok_analysis)

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
