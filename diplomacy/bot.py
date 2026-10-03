import os
import re
import json
import time
import html
from datetime import datetime, timezone

import requests
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")  # необов'язково — без нього бот працює в простому режимі

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 4
MAX_ENTRIES_CHECKED_PER_RUN = 20  # скільки свіжих новин максимум прогнати через фільтр релевантності за раз
REQUEST_TIMEOUT = 20

ARTICLE_FETCH_TIMEOUT = 15
ARTICLE_MAX_CHARS = 4000

# Джерела: (URL, Назва джерела, чи потрібен переклад)
# Офіційні першоджерела — прес-служби держустанов та міжнародних організацій.
FEEDS = [
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Council of the EU", True),
    ("https://news.un.org/feed/subscribe/en/news/all/rss.xml", "UN News", True),
    ("https://www.state.gov/press-releases/feed/", "U.S. Department of State", True),
    ("https://www.whitehouse.gov/feed/", "The White House", True),
]

GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()


def fetch_article_text(url):
    """Намагається витягти повний текст новини зі сторінки. Повертає '' при невдачі."""
    try:
        resp = requests.get(
            url,
            timeout=ARTICLE_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DiplomacyBot/1.0)"},
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


# ===== Історія публікацій (і пропущених нерелевантних новин) =====
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
        json.dump(history[-500:], f, ensure_ascii=False, indent=2)


# ===== Groq: фільтр релевантності + переклад + коротка дипломатична довідка =====
def analyze_with_groq(title, article_text, source_name):
    """Повертає dict {"relevant": bool, "title": str|None, "analysis": str|None}.

    relevant=True лише якщо новина дійсно про зустріч, візит, телефонну розмову,
    саміт чи підписання угоди між офіційними особами різних країн або
    між країною та міжнародною організацією. Усе інше (привітання зі святом,
    внутрішня політика, санкційні технічні деталі тощо) — relevant=False.

    Якщо GROQ_API_KEY відсутній — повертає relevant=True з порожнім аналізом
    (бот просто публікує все підряд, без фільтра і без перекладу).
    """
    if not GROQ_API_KEY:
        return {"relevant": True, "title": None, "analysis": None}

    prompt = (
        "Ти — редактор дипломатичних новин для українського Telegram-каналу, який висвітлює "
        "саме зустрічі, візити, телефонні розмови, саміти та підписання угод між офіційними "
        "особами різних країн (президенти, прем'єри, міністри закордонних справ) або між "
        "країною та міжнародною організацією (ООН, ЄС, НАТО тощо).\n\n"
        f"Джерело: {source_name}.\n"
        f"Оригінальний заголовок: {title}\n\n"
        f"Текст новини:\n{article_text}\n\n"
        "Крок 1: Визнач, чи ця новина ДІЙСНО про конкретну дипломатичну зустріч/візит/дзвінок/"
        "саміт/підписання угоди (а не просто заява, вітання зі святом, санкції, внутрішня "
        "політика чи загальна аналітика без конкретної зустрічі). Якщо ні — поверни "
        '{"relevant": false, "title": null, "analysis": null} і більше нічого.\n\n'
        "Крок 2 (лише якщо relevant=true):\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів): хто з ким зустрівся/говорив "
        "і про що.\n"
        "2) Дай 2-3 речення дипломатичного коментаря українською на основі фактів зі статті: "
        "хто брав участь, яка головна тема, які домовленості чи результати, якщо згадані.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"relevant": true, "title": "...", "analysis": "..."}'
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
            print(f"Groq: HTTP {resp.status_code}: {resp.text[:500]}")
            return {"relevant": True, "title": None, "analysis": None}
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return {
            "relevant": bool(parsed.get("relevant", True)),
            "title": parsed.get("title"),
            "analysis": parsed.get("analysis"),
        }
    except Exception as e:
        print(f"Groq: не вдалося обробити новину ({e})")
        # Якщо щось пішло не так — краще показати новину як є, ніж втратити її.
        return {"relevant": True, "title": None, "analysis": None}


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

        if getattr(feed, "bozo", False) and not feed.entries:
            print(f"Фід порожній або некоректний: {feed_url} ({feed.get('bozo_exception')})")
            continue

        for entry in feed.entries[:6]:
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
        f"🗓 {date_str} | 🏛 {html.escape(entry['source'])}",
    ]

    if groq_analysis:
        parts += ["", f"🤝 {html.escape(groq_analysis)}"]

    parts += ["", f"<a href='{html.escape(entry['link'])}'>Читати першоджерело</a>"]
    return "\n".join(parts)


def main():
    history = load_history()
    entries = collect_entries()
    new_posts = 0
    checked = 0

    for entry in entries:
        if new_posts >= MAX_POSTS_PER_RUN or checked >= MAX_ENTRIES_CHECKED_PER_RUN:
            break
        if entry["link"] in history:
            continue

        checked += 1

        article_text = fetch_article_text(entry["link"])
        if not article_text:
            article_text = entry["summary"]

        result = analyze_with_groq(entry["title"], article_text, entry["source"])

        if not result["relevant"]:
            # Не про зустріч — пропускаємо, але запам'ятовуємо, щоб не перевіряти повторно.
            history.append(entry["link"])
            print(f"Пропущено (не дипломатична зустріч): {entry['title']}")
            continue

        message = format_message(entry, result["title"], result["analysis"])

        if send_to_telegram(message):
            history.append(entry["link"])
            new_posts += 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(3)
        else:
            print(f"Не вдалося опублікувати: {entry['title']}")

    save_history(history)
    print(f"Готово. Перевірено: {checked}, опубліковано: {new_posts}")


if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        main()
    else:
        print("Помилка: не знайдені TELEGRAM_TOKEN або DIPLOMACY_CHANNEL_ID/CHANNEL_ID")
