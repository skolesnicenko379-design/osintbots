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
    history["links"] = history["links"][-800:]
    history["recent_posts"] = history["recent_posts"][-RECENT_POSTS_FOR_DEDUP:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def analyze_with_groq(title, article_text, source_name, recent_posts):
    if not GROQ_API_KEY:
        return {"relevant": True, "duplicate": False, "title": None, "analysis": None}

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
        "ОСТАННІ ОПУБЛІКОВАНІ ПОСТИ (для перевірки на дублі):\n"
        f"{recent_block}\n\n"
        "Виконай завдання:\n"
        "Крок 1 (relevant): чи є ця подія значущою для європейської/трансатлантичної геополітики або міжнародних відносин? "
        "Якщо це рутина, дрібний кримінал або вузька локальна внутрішня тема — поверни "
        '{"relevant": false, "duplicate": false, "title": null, "analysis": null}.\n\n'
        "Крок 2 (duplicate): чи дублює ця новина ту саму подію/саміт, про яку вже повідомлялося в останніх постах вище? "
        'Якщо так — поверни {"relevant": true, "duplicate": true, "title": null, "analysis": null}.\n\n'
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
