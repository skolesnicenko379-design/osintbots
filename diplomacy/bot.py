import os
import re
import json
import time
import html
import traceback
from datetime import datetime, timezone, timedelta

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
MAX_ENTRIES_CHECKED_PER_RUN = 50
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24  # Тільки свіжі матеріали за останні 24 години
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 20
ARTICLE_MAX_CHARS = 4000
GROQ_TIMEOUT = 45          # 120b-модель відповідає довше, ніж звичайний REQUEST_TIMEOUT
GROQ_MAX_RETRIES = 2
GROQ_RETRY_DELAY = 4       # базова пауза між спробами (секунди)
GROQ_CALL_DELAY = 1.5      # пауза ПЕРЕД кожним викликом Groq, щоб не впертися в rate limit

# Розширена та збалансована база геополітичних джерел
FEEDS = [
    # --- Інституції ЄС ---
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", True),
    ("https://ec.europa.eu/commission/presscorner/api/rss?language=en", "Єврокомісія", True),
    ("https://www.eeas.europa.eu/rss.xml", "EEAS (Дипломатія ЄС)", True),
    ("https://www.europarl.europa.eu/rss/doc/top-stories/en.xml", "Європарламент", True),

    # --- Провідні європейські держави ---
    ("https://www.bundesregierung.de/breg-en/service/rss", "Уряд Німеччини", True),
    ("https://www.bundestag.de/includes/rss/Bundestag_A-Z.xml", "Бундестаг", True),
    ("https://www.diplomatie.gouv.fr/spip.php?page=backend&id_rubrique=260", "МЗС Франції", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=foreign-commonwealth-development-office", "FCDO (Британія)", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=prime-ministers-office-10-downing-street", "Даунінг-стріт", True),
    ("https://www.gov.pl/feed/rss/diplomacy", "МЗС Польщі", True),
    ("https://www.esteri.it/en/feed/", "МЗС Італії", True),
    ("https://mfa.gov.ua/rss", "МЗС України", False),
    ("https://www.president.gov.ua/news/rss", "Офіс Президента України", False),

    # --- Трансатлантичні партнери та альянси ---
    ("https://www.state.gov/press-releases/feed/", "Держдеп США", True),
    ("https://www.whitehouse.gov/briefing-room/feed/", "Білий дім", True),
    ("https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?max=10&Categories=Press%20Releases", "Пентагон", True),
    ("https://www.nato.int/cps/en/natohq/news.xml", "НАТО", True),

    # --- Багатосторонні структури та фінанси ---
    ("https://www.osce.org/rss", "ОБСЄ", True),
    ("https://press.un.org/en/rss.xml", "ООН (Прес-центр)", True),
    ("https://www.imf.org/en/News/RSS", "МВФ", True),
    ("https://www.worldbank.org/en/news/press-release.rss", "Світовий банк", True),
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
    if not text:
        return ""
    text = html.unescape(text)
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
    # Зберігаємо до 1200 посилань для надійного захисту від повторів
    history["links"] = list(dict.fromkeys(history["links"]))[-1200:]
    history["recent_posts"] = history["recent_posts"][-RECENT_POSTS_FOR_DEDUP:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def analyze_with_groq(title, article_text, source_name, recent_posts):
    # Якщо ключ взагалі не налаштований — це свідомий режим "без фільтрації",
    # а не збій: публікуємо як є (без перекладу/аналізу) без пропуску.
    if not GROQ_API_KEY:
        return {"relevant": True, "duplicate": False, "title": None, "analysis": None, "failed": False}

    recent_block = "(поки що порожньо — це перша перевірка)"
    if recent_posts:
        recent_block = "\n".join(
            f"- [{p['source']}] {p['title']}: {p['summary']}" for p in recent_posts
        )

    prompt = (
        "Ти — аналітик та редактор європейського й трансатлантичного геополітичного каналу. "
        "Твоя мета: відбирати та аналізувати ключові міжнародні події, міждержавні переговори, "
        "двосторонні та багатосторонні саміти, альянси (НАТО, ЄС, G7), рішення з безпеки й оборони, "
        "санкційну політику та макроекономічні зсуви.\n\n"
        "ФОКУС ТА КРИТЕРІЇ ВІДБОРУ:\n"
        "1. Геополітика Європи та Заходу: пріоритет мають події в країнах ЄС, Великій Британії, США, "
        "країнах Східної та Північної Європи, а також їхня спільна зовнішня політика.\n"
        "2. Рівень акторів: важливими є не лише чинні глави держав і міністри, а й впливові політичні лідери, "
        "керівники провідних партій, парламентські делегації, очільники Єврокомісії, НАТО та дипломатичних місій.\n"
        "3. Локальний шум: відсіюй суто внутрішньополітичні дрібні суперечки, рутинні бюрократичні звіти "
        "та події між країнами Азії, Африки чи Латинської Америки, якщо в них немає прямого зв'язку з європейською "
        "безпекою чи західною дипломатією.\n\n"
        f"Джерело: {source_name}\n"
        f"Заголовок: {title}\n\n"
        f"Текст статті:\n{article_text}\n\n"
        "ОСТАННІ ОПУБЛІКОВАНІ ПОСТИ (для суворої перевірки на смисловий дубль):\n"
        f"{recent_block}\n\n"
        "Виконай завдання:\n"
        "Крок 1 (relevant): чи є ця подія значущою для європейської/трансатлантичної геополітики або міжнародних відносин? "
        "Якщо це рутина, дрібний кримінал або вузька локальна внутрішня тема — поверни "
        '{"relevant": false, "duplicate": false, "title": null, "analysis": null}.\n\n'
        "Крок 2 (duplicate): чи описує ця новина ТУ САМУ подію, зустріч, саміт чи заяву, яка вже була опублікована вище? "
        'Якщо так — обов\'язково поверни {"relevant": true, "duplicate": true, "title": null, "analysis": null}.\n\n'
        "Крок 3 (якщо relevant=true і duplicate=false):\n"
        "- Сформулюй лаконічний, інформативний заголовок українською (до 14 слів).\n"
        "- Напиши стислий аналітичний коментар (2–3 речення) про геополітичне значення події.\n\n"
        "Відповідай ВИКЛЮЧНО валідним JSON-об'єктом без markdown-блоків:\n"
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

    last_error = None

    for attempt in range(1, GROQ_MAX_RETRIES + 2):  # перша спроба + N ретраїв
        time.sleep(GROQ_CALL_DELAY)  # невелика пауза перед КОЖНИМ зверненням до Groq
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)

            if resp.status_code == 429:
                retry_after = GROQ_RETRY_DELAY
                try:
                    retry_after = float(resp.headers.get("retry-after", GROQ_RETRY_DELAY))
                except (TypeError, ValueError):
                    pass
                last_error = f"Groq 429 (rate limit), спроба {attempt}, чекаю {retry_after}с"
                print(last_error)
                time.sleep(retry_after)
                continue

            if resp.status_code != 200:
                last_error = f"Groq: HTTP {resp.status_code}: {resp.text[:500]}"
                print(last_error)
                time.sleep(GROQ_RETRY_DELAY)
                continue

            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            text = text.replace("`" * 3 + "json", "").replace("`" * 3, "").strip()

            parsed = json.loads(text)
            return {
                "relevant": bool(parsed.get("relevant", True)),
                "duplicate": bool(parsed.get("duplicate", False)),
                "title": parsed.get("title"),
                "analysis": parsed.get("analysis"),
                "failed": False,
            }
        except Exception as e:
            last_error = f"Groq: помилка обробки (спроба {attempt}): {e}"
            print(last_error)
            time.sleep(GROQ_RETRY_DELAY)
            continue

    # Усі спроби вичерпано — НЕ публікуємо наосліп (без fail-open):
    # новина просто повернеться в наступному прогоні.
    notify_admin(f"Groq не відповів для статті «{title}» після {GROQ_MAX_RETRIES + 1} спроб.\n{last_error}")
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
            print(f"Telegram rate limit, чекаю {retry_after}с")
            time.sleep(retry_after)
            response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        return response.status_code == 200
    except Exception as e:
        print(f"Помилка запиту до Telegram: {e}")
        notify_admin(f"Помилка з'єднання з Telegram API: {e}")
        return False


def collect_entries():
    all_entries = []
    now_utc = datetime.now(timezone.utc)
    max_age_delta = timedelta(hours=MAX_ARTICLE_AGE_HOURS)

    for feed_url, source_name, needs_translation in FEEDS:
        try:
            resp = requests.get(feed_url, timeout=15, impersonate="chrome120")
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"Не вдалося завантажити фід {source_name} ({feed_url}): {e}")
            continue

        if getattr(feed, "bozo", False) and not feed.entries:
            continue

        for entry in feed.entries[:10]:
            link = entry.get("link")
            if not link:
                continue

            # Обробка дати публікації
            published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if not published_struct:
                # Якщо дати немає взагалі — ігноруємо, щоб не тягнути застарілі архіви
                continue

            published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)

            # ФІЛЬТР: Тільки публікації за останні 24 години
            if (now_utc - published_dt) > max_age_delta:
                continue

            all_entries.append({
                "link": link.strip(),
                "title": entry.get("title", "Без заголовка"),
                "summary": strip_html(entry.get("summary", ""))[:1500],
                "source": source_name,
                "published": published_dt,
            })

    # Сортування: від найсвіжіших до старіших
    all_entries.sort(key=lambda e: e["published"], reverse=True)
    return all_entries


def format_message(entry, groq_title, groq_analysis):
    raw_title = groq_title or entry["title"]
    clean_title = clean_text(raw_title)
    safe_title = html.escape(clean_title)
    
    date_str = entry["published"].strftime("%d.%m.%Y")
    safe_source = html.escape(entry['source'])

    parts = [
        f"<b>{safe_title}</b>",
        "",
        f"🗓 {date_str} | 🏛 {safe_source}",
    ]

    if groq_analysis:
        clean_analysis = clean_text(groq_analysis)
        safe_analysis = html.escape(clean_analysis)
        parts += ["", f"🤝 {safe_analysis}"]

    safe_link = entry['link'].replace('"', '%22')
    parts += ["", f'<a href="{safe_link}">Читати першоджерело</a>']
    return "\n".join(parts)


def main():
    history = load_history()
    entries = collect_entries()
    new_posts = 0
    checked = 0

    for entry in entries:
        if new_posts >= MAX_POSTS_PER_RUN or checked >= MAX_ENTRIES_CHECKED_PER_RUN:
            break
        
        # Перевірка на унікальність лінка
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
            # Groq тимчасово недоступний для цієї статті — НЕ позначаємо як
            # оброблену, щоб повторити спробу в наступному прогоні.
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
