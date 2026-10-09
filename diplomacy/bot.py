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
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip().replace('"', '').replace("'", "")
CHANNEL_ID = (os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip().replace('"', '').replace("'", "")

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 4
REQUEST_TIMEOUT = 20

# Джерела: (URL, Назва джерела, чи потрібен переклад)
FEEDS = [
    ("https://mil.in.ua/uk/news/feed/", "mil.in.ua", False),
    ("https://defence-ua.com/rss.xml", "defence-ua.com", False),
    ("https://breakingdefense.com/feed/", "Breaking Defense", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/7402252502089930204", "Western Defense Industry", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/17810137244338497811", "Global MilTech", True),
]

# АКТУАЛЬНІ РОБОЧІ МОДЕЛІ GROQ
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it"
]
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

ARTICLE_FETCH_TIMEOUT = 15
ARTICLE_MAX_CHARS = 4000

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

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
    except Exception as e:
        print(f"Не вдалося завантажити текст статті ({url}): {e}")
        return ""

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, list):
                    return {"links": data, "recent_posts": []}
                return data
            except json.JSONDecodeError:
                return {"links": [], "recent_posts": []}
    return {"links": [], "recent_posts": []}

def save_history(history):
    history["links"] = list(dict.fromkeys(history["links"]))[-1200:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def _is_model_dead_error(status_code, resp_text):
    if status_code == 404:
        return True
    if status_code == 400 and ("model_decommissioned" in (resp_text or "") or "does not exist" in (resp_text or "")):
        return True
    return False

def enrich_with_groq(title, article_text, source_name, needs_translation):
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
        "Виконай ДВІ речі на основі ЗМІСТУ новини:\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів), без клікбейту.\n"
        "2) Дай 2-3 речення технічного/аналітичного коментаря українською: що саме сталося, "
        "які характеристики техніки згадуються, який можливий військовий вплив.\n"
        "КРИТИЧНО: Поле 'analysis' НІКОЛИ не повинно бути порожнім.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"title": "...", "analysis": "..."}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    
    for model in GROQ_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        for attempt in range(2):
            try:
                resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
                
                if _is_model_dead_error(resp.status_code, resp.text):
                    print(f"Groq: модель {model} недоступна")
                    break
                    
                if resp.status_code == 429:
                    time.sleep(5)
                    continue
                    
                if resp.status_code != 200:
                    time.sleep(3)
                    continue

                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                parsed = json.loads(text)
                
                ai_title = parsed.get("title")
                ai_analysis = parsed.get("analysis")
                
                if not ai_analysis or len(ai_analysis.strip()) < 5:
                    ai_analysis = "Додаткові технічні деталі уточнюються експертами."
                    
                return ai_title, ai_analysis
                
            except Exception as e:
                print(f"Groq ({model}): помилка ({e})")
                time.sleep(3)
                continue
            
    return None, None

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
        return response.status_code == 200
    except requests.RequestException as e:
        print(f"Помилка запиту до Telegram: {e}")
        return False

def collect_entries():
    all_entries = []
    for feed_url, source_name, needs_translation in FEEDS:
        try:
            time.sleep(1)
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
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

    if groq_analysis and len(groq_analysis.strip()) > 5:
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
        if entry["link"] in history["links"]:
            continue

        article_text = fetch_article_text(entry["link"])
        if not article_text or len(article_text) < 50:
            article_text = entry["summary"]

        groq_title, groq_analysis = enrich_with_groq(
            entry["title"], article_text, entry["source"], entry["needs_translation"]
        )

        message = format_message(entry, groq_title, groq_analysis)

        if send_to_telegram(message):
            history["links"].append(entry["link"])
            new_posts += 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(3)
        else:
            print(f"Не вдалося опублікувати: {entry['title']}")

    save_history(history)
    print(f"Готово. Опубліковано новин: {new_posts}")

if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        main()
    else:
        print("Помилка: не знайдені TELEGRAM_TOKEN або CHANNEL_ID")
