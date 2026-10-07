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
# Для API Anthropic та Telegram ми використаємо надійний вбудований urllib.
from curl_cffi import requests as curl_requests
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
# Використовуємо .strip() щоб видалити випадкові \n (Enter) або пробіли,
# які могли потрапити при копіюванні ключа в GitHub Secrets.
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
CHANNEL_ID = (os.environ.get("DIPLOMACY_CHANNEL_ID") or os.environ.get("CHANNEL_ID") or "").strip()
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 50
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24  # Тільки свіжі матеріали за останні 24 години
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 20
ARTICLE_MAX_CHARS = 4000

# Оптимізовані налаштування для ПЛАТНОГО акаунту Anthropic
ANTHROPIC_TIMEOUT = 45          # пауза очікування відповіді моделі
ANTHROPIC_MAX_RETRIES = 2
ANTHROPIC_RETRY_DELAY = 2       # базова пауза між спробами (секунди)
ANTHROPIC_CALL_DELAY = 0.2      # мікропауза ПЕРЕД кожним викликом API (швидкий режим)

# Джерела, що мають стабільний власний RSS (перевірено — не падають з 404/403)
FEEDS = [
    # --- Інституції ЄС ---
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", True),
    ("https://ec.europa.eu/commission/presscorner/api/rss?language=en", "Єврокомісія", True),
    ("https://www.europarl.europa.eu/rss/doc/top-stories/en.xml", "Європарламент", True),

    # --- Провідні європейські держави ---
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=foreign-commonwealth-development-office", "FCDO (Британія)", True),
    ("https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=prime-ministers-office-10-downing-street", "Даунінг-стріт", True),
    ("https://www.gov.pl/feed/rss/diplomacy", "МЗС Польщі", True),
    ("https://www.esteri.it/en/feed/", "МЗС Італії", True),
]

# Бот спробує першу модель; якщо сервер відповість 404 (Not Found), 
# перейде до наступної стабільної датованої версії. Ми починаємо з 3.5, але
# маємо 100% робочий бекап з 3 серії (Haiku).
ANTHROPIC_MODELS = [
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-haiku-20240307",
    "claude-3-sonnet-20240229"
]
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MAX_TOKENS = 1024

# ===== Google News RSS (site:) замість мертвих/нестабільних офіційних RSS =====
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
        resp = curl_requests.get(
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
        "Ти — старший геополітичний аналітик і редактор трансатлантичного дипломатичного каналу, "
        "який пише для фахової аудиторії: дипломатів, аналітиків think tank'ів та журналістів-міжнародників. "
        "Твоя мета — відбирати значущі міжнародні події та давати їм експертну, технічно точну оцінку, "
        "а не переказ новини своїми словами.\n\n"
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
        "- Сформулюй лаконічний, фактологічно точний заголовок українською (до 14 слів), без публіцистичних штампів.\n"
        "- Напиши аналітичний коментар (3–5 речень) у реєстрі експертного брифінгу, а не журналістського переказу. "
        "Обов'язково врахуй, де доречно:\n"
        "  • конкретний інституційний/правовий механізм події (назва угоди, формату перемовин, санкційного пакета, "
        "статті договору, мандата місії тощо), а не загальні фрази;\n"
        "  • розстановку інтересів сторін і можливі розбіжності між учасниками, якщо вони є;\n"
        "  • найближчі практичні наслідки або подальші кроки (наступний раунд перемовин, голосування, ратифікація, "
        "дедлайн, очікувана реакція третіх держав);\n"
        "  • за наявності — цифри, суми, терміни чи конкретні зобов'язання, згадані в тексті.\n"
        "Уникай оціночних кліше на кшталт «підкреслює важливість» чи «демонструє підтримку» без конкретики, "
        "що саме за ними стоїть.\n\n"
        "Відповідай ВИКЛЮЧНО валідним JSON-об'єктом без markdown-блоків:\n"
        '{"relevant": true, "duplicate": false, "title": "...", "analysis": "..."}'
    )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    last_error = None

    # Перебираємо список моделей, поки не знайдемо ту, яка працює
    for current_model in ANTHROPIC_MODELS:
        payload = {
            "model": current_model,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        data_bytes = json.dumps(payload).encode("utf-8")

        for attempt in range(1, ANTHROPIC_MAX_RETRIES + 2):
            time.sleep(ANTHROPIC_CALL_DELAY)
            try:
                req = urllib.request.Request(
                    ANTHROPIC_API_URL,
                    data=data_bytes,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=ANTHROPIC_TIMEOUT) as response:
                    response_body = response.read().decode("utf-8")
                    data = json.loads(response_body)

                    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
                    text = "".join(text_blocks).strip()
                    text = text.replace("`" * 3 + "json", "").replace("`" * 3, "").strip()

                    parsed = json.loads(text)
                    return {
                        "relevant": bool(parsed.get("relevant", True)),
                        "duplicate": bool(parsed.get("duplicate", False)),
                        "title": parsed.get("title"),
                        "analysis": parsed.get("analysis"),
                        "failed": False,
                    }
            except urllib.error.HTTPError as e:
                if e.code == 404: 
                    # Якщо модель не знайдена, перериваємо ретраї і йдемо до наступної моделі
                    print(f"Модель {current_model} не знайдена (404). Пробую наступну...")
                    break 
                
                if e.code == 429:
                    retry_after = ANTHROPIC_RETRY_DELAY
                    try:
                        retry_after = float(e.headers.get("retry-after", ANTHROPIC_RETRY_DELAY))
                    except:
                        pass
                    last_error = f"Anthropic 429 (rate limit) для {current_model}, чекаю {retry_after}с"
                    print(last_error)
                    time.sleep(retry_after)
                    continue
                else:
                    error_body = e.read().decode("utf-8")
                    last_error = f"Anthropic HTTP {e.code} для {current_model}: {error_body[:500]}"
                    print(last_error)
                    time.sleep(ANTHROPIC_RETRY_DELAY)
                    continue
            except Exception as e:
                last_error = f"Anthropic помилка для {current_model} (спроба {attempt}): {e}"
                print(last_error)
                time.sleep(ANTHROPIC_RETRY_DELAY)
                continue

    # Якщо ми пройшли всі моделі і всі спроби провалилися
    notify_admin(f"Anthropic API не відповів для статті «{title}» на жодній з моделей після всіх спроб.\nОстання помилка: {last_error}")
    return {"relevant": False, "duplicate": False, "title": None, "analysis": None, "failed": True}


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            return response.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                resp_data = json.loads(e.read().decode("utf-8"))
                retry_after = resp_data.get("parameters", {}).get("retry_after", 5)
            except:
                retry_after = 5
            print(f"Telegram rate limit, чекаю {retry_after}с")
            time.sleep(retry_after)
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response2:
                    return response2.status == 200
            except:
                return False
        print(f"Помилка запиту до Telegram: HTTP {e.code}")
        return False
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
            resp = curl_requests.get(feed_url, timeout=REQUEST_TIMEOUT, impersonate="chrome120")
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

    if ai_analysis:
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

        result = analyze_with_claude(
            entry["title"], article_text, entry["source"], history["recent_posts"]
        )

        if result.get("failed"):
            print(f"Пропущено тимчасово (Anthropic API не відповів): {entry['title']}")
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
