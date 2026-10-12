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
CHANNEL_ID = (os.environ.get("GEOECONOMICS_CHANNEL_ID") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip().replace('"', '').replace("'", "")
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 15  
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 15
ARTICLE_MAX_CHARS = 4000
GROQ_TIMEOUT = 45
GROQ_CALL_DELAY = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# --- ДЖЕРЕЛА ГЕОЕКОНОМІЧНОГО БОТА ---
search_queries = [
    ("Federal Reserve OR FOMC OR Jerome Powell economy when:24h", "ФРС США"),
    ("PBOC OR People's Bank of China OR yuan liquidity when:24h", "Центробанк Китаю"),
    ("Bank of Japan OR BOJ OR yen policy when:24h", "Банк Японії"),
    ("US Treasury sanctions OR export controls China when:24h", "Мінфін США"),
    ("global oil markets OR IEA report OR critical minerals when:24h", "Енергетичні ринки"),
    ("OPEC+ production OR natural gas prices Europe when:24h", "Сировинні ринки")
]

FEEDS = []
for query, label in search_queries:
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    FEEDS.append((url, label, True))

FEEDS.extend([
    ("https://www.imf.org/en/News/RSS", "МВФ", True),
    ("https://www.wto.org/english/res_e/wto_rss_e.xml", "СОТ", True),
    ("https://www.bis.org/doclist/all_pressrels.rss", "BIS", True),
    ("https://www.ecb.europa.eu/rss/press.html", "ЄЦБ", True),
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", True),
    ("https://search.worldbank.org/api/v2/news?format=atom", "Світовий банк", True),
])

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

def get_available_groq_models():
    if not GROQ_API_KEY:
        return []
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        resp = requests.get(GROQ_MODELS_URL, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        models = [item["id"] for item in data.get("data", [])]
        
        valid_models = []
        for m in models:
            ml = m.lower()
            # Відкидаємо аудіо, фільтри безпеки та візуальні моделі
            if "guard" in ml or "orpheus" in ml or "whisper" in ml or "vision" in ml:
                continue
            valid_models.append(m)
        
        # Сортуємо: спочатку Llama, потім Mixtral, потім інші
        valid_models.sort(key=lambda x: (0 if "llama" in x.lower() else 1, x))
        
        print(f"Доступні текстові моделі Groq: {valid_models[:5]}...")
        return valid_models
    except Exception as e:
        print(f"Не вдалося отримати список моделей Groq: {e}")
        return []

def notify_admin(message):
    if not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    text = f"⚠️ <b>Помилка GeoEconomics Bot:</b>\n\n<pre>{html.escape(message[:3500])}</pre>"
    payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass

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

def analyze_with_groq(title, article_text, source_name, recent_posts, active_models):
    if not GROQ_API_KEY or not active_models:
        print("КРИТИЧНА ПОМИЛКА: Немає API-ключа або доступних моделей!")
        return {"relevant": False, "duplicate": False, "failed": True}

    recent_block = "(поки що порожньо)"
    if recent_posts:
        recent_block = "\n".join(
            f"- [{p['source']}] {p['title']}: {p['summary']}" for p in recent_posts
        )

    prompt = (
        "Ти — старший макроекономічний аналітик. Твоє завдання: перетворити новину на сухий, технічний дайджест для інвесторів та політиків.\n\n"
        f"Джерело: {source_name}\n"
        f"Заголовок: {title}\n\n"
        f"Текст статті:\n{article_text}\n\n"
        "ОСТАННІ ПОСТИ (перевірка на дублікати):\n"
        f"{recent_block}\n\n"
        "ПРАВИЛА АНАЛІЗУ:\n"
        "1. Якщо подія нерелевантна (дрібні новини) — поверни relevant: false.\n"
        "2. Якщо це дубль події з останніх постів — поверни duplicate: true.\n"
        "3. Якщо новина важлива, створи поле 'title' (до 10 слів, суть події) та поле 'analysis'.\n\n"
        "ВИМОГИ ДО ПОЛЯ 'analysis':\n"
        "- Текст має бути ОДНИМ суцільним абзацом (без розривів, маркерів чи абзаців).\n"
        "- Обсяг: 3-4 речення. Максимум 500 символів.\n"
        "- СТРУКТУРА АБЗАЦУ (пиши злито):\n"
        "  [Факт події]. [Технічні деталі: цифри, обсяги, зміна ставок чи умов]. [Макроекономічний висновок: вплив на ліквідність, боргові ринки, ланцюги постачань або курси валют].\n"
        "- ЗАБОРОНЕНО: емоційні слова, повторення одного й того ж речення, фрази на кшталт 'це вплине на економіку стратегічно'. Використовуй виключно професійний фінансовий жаргон (спред, маржа, волатильність, ліквідність, девальвація, яструбина/голубина політика).\n\n"
        "Відповідай ТІЛЬКИ чистим JSON форматом:\n"
        '{"relevant": true, "duplicate": false, "title": "Унікальний заголовок", "analysis": "Твій єдиний сухий абзац."}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    # Пробуємо до 3-х перших доступних моделей
    for model in active_models[:3]:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            # Вилучено strict JSON mode, бо Allam-2-7b через нього видає помилку
        }

        time.sleep(GROQ_CALL_DELAY)
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 5))
                time.sleep(retry_after)
                continue

            if resp.status_code != 200:
                print(f"Groq ({model}): HTTP {resp.status_code}: {resp.text[:300]}")
                continue

            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            
            # Парсимо JSON із тексту за допомогою регулярки
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
            else:
                parsed = json.loads(text)

            ai_title = parsed.get("title", "")
            ai_analysis = parsed.get("analysis", "")

            if "Унікальний заголовок" in ai_title or "Коментар щодо" in ai_analysis:
                raise ValueError("Модель повернула шаблонний текст.")

            return {
                "relevant": bool(parsed.get("relevant", True)),
                "duplicate": bool(parsed.get("duplicate", False)),
                "title": ai_title if len(ai_title) > 5 else title,
                "analysis": ai_analysis,
                "failed": False,
            }
        except Exception as e:
            print(f"Groq ({model}): помилка обробки: {e}")
            continue

    print(f"Groq не зміг опрацювати статтю «{title}».")
    return {"relevant": False, "duplicate": False, "title": None, "analysis": None, "failed": True}

def send_to_telegram(text):
    if not CHANNEL_ID:
        print("Помилка: GEOECONOMICS_CHANNEL_ID порожній!")
        return False
        
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
            
        if response.status_code != 200:
            print(f"Telegram відхилив повідомлення: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"Помилка запиту до Telegram: {e}")
        return False

def collect_entries():
    all_entries = []
    now_utc = datetime.now(timezone.utc)
    max_age_delta = timedelta(hours=MAX_ARTICLE_AGE_HOURS)

    for feed_url, source_name, _ in FEEDS:
        try:
            time.sleep(1)
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            if resp.status_code == 403:
                print(f"Увага: {source_name} повернув 403. Блокування доступу.")
                continue
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
        parts += ["", f"{safe_analysis}"]

    safe_link = entry['link'].replace('"', '%22')
    parts += ["", f'<a href="{safe_link}">Читати першоджерело</a>']
    return "\n".join(parts)

def main():
    history = load_history()
    entries = collect_entries()
    print(f"Зібрано свіжих записів: {len(entries)}")

    active_models = get_available_groq_models()
    if not active_models:
        print("Не знайдено жодної доступної моделі Groq. Завершення роботи.")
        return

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
            entry["title"], article_text, entry["source"], history["recent_posts"], active_models
        )

        if result.get("failed"):
            print(f"Пропущено тимчасово (помилка ШІ): {entry['title']}")
            continue

        if not result["relevant"]:
            history["links"].append(entry["link"])
            print(f"Відхилено (нерелевантне): {entry['title']}")
            continue

        if result["duplicate"]:
            history["links"].append(entry["link"])
            print(f"Відхилено (дублікат): {entry['title']}")
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
            print(f"Помилка публікації: {entry['title']}")

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
        print("Помилка: TELEGRAM_TOKEN або GEOECONOMICS_CHANNEL_ID не задано.")
