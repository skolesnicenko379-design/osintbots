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

import sys
sys.path.insert(0, os.path.abspath(".."))
from shared_dedup import (
    load_shared_history, save_shared_history, is_duplicate_link,
    is_duplicate_title, select_relevant_recent_posts, record_published, canonical_link,
)

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip().replace('"', '').replace("'", "")
CHANNEL_ID = (os.environ.get("GEOECONOMICS_CHANNEL_ID") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip().replace('"', '').replace("'", "")
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip().replace('"', '').replace("'", "")
ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 12
RECENT_POSTS_FOR_DEDUP = 20

# Рівномірний розподіл випуску між регіонами та транспортними коридорами.
GROUP_ORDER = ["china", "russia", "eu", "us", "corridors", "global", "japan"]
MAX_CHECKED_PER_GROUP = {
    "china": 3, "russia": 3, "eu": 3, "us": 3,
    "corridors": 3, "global": 3, "japan": 1,
}
MAX_POSTS_PER_GROUP = {
    "china": 2, "russia": 2, "eu": 2, "us": 2,
    "corridors": 2, "global": 2, "japan": 1,
}
MAX_ARTICLE_AGE_HOURS = 24
REQUEST_TIMEOUT = 12
ARTICLE_FETCH_TIMEOUT = 8
ARTICLE_MAX_CHARS = 3000
GROQ_TIMEOUT = 15
GROQ_CALL_DELAY = 1
GROQ_MAX_MODELS_TO_TRY = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# --- ДЖЕРЕЛА ГЕОЕКОНОМІЧНОГО БОТА ---
# Google News RSS агрегує матеріали різних видань; фактичне видання беремо з RSS,
# а тему пошукового запиту показуємо окремо.
search_queries = [
    # Китай: макроекономіка, монетарна політика, торгівля та промислові заходи.
    ("PBOC OR \"People's Bank of China\" OR China economy OR Chinese exports OR property sector OR consumer demand when:24h",
     "Китай · макроекономіка та монетарна політика", "china"),
    ("China trade agreement OR MOFCOM OR China tariffs OR export controls OR critical minerals when:24h",
     "Китай · торгівля, угоди й обмеження", "china"),
    ("site:english.mofcom.gov.cn OR site:pbc.gov.cn/en China trade policy economic measures when:24h",
     "Китай · офіційні економічні рішення", "china"),

    # Росія: макроекономічні показники, бюджет, експорт та рішення державних інституцій.
    ("Russia economy OR Bank of Russia OR Russian central bank OR ruble inflation OR Russian budget OR oil exports when:24h",
     "РФ · економіка, бюджет і експорт", "russia"),
    ("site:cbr.ru OR site:minfin.gov.ru Russia monetary policy budget economy when:24h",
     "РФ · офіційні економічні рішення", "russia"),

    # США: ставки, інфляція, бюджет, торгівля, санкції та промислова політика.
    ("Federal Reserve OR FOMC OR US inflation OR US employment OR Treasury yields OR US GDP when:24h",
     "США · макроекономіка та ФРС", "us"),
    ("site:home.treasury.gov OR site:ustr.gov OR US trade agreement OR tariffs OR export controls OR industrial policy when:24h",
     "США · торгівля, санкції й промисловість", "us"),

    # ЄС: макропоказники, конкурентоспроможність, торговельна політика та угоди.
    ("European Union economy OR European Commission competitiveness OR Eurostat GDP OR eurozone inflation OR EU industrial policy when:24h",
     "ЄС · макроекономіка та промисловість", "eu"),
    ("EU trade agreement OR EU tariffs OR EU-China trade OR European Commission economic security OR EU sanctions when:24h",
     "ЄС · торгівля, угоди й економічна безпека", "eu"),

    # Міжнародні транспортно-логістичні коридори: проєкти, фінансування, меморандуми й угоди.
    ("India Middle East Europe Economic Corridor OR IMEC when:24h",
     "Коридори · IMEC", "corridors"),
    ("Trans-Caspian International Transport Route OR Middle Corridor OR Trans-Caspian Transport Corridor OR TITR when:24h",
     "Коридори · Середній і Транскаспійський", "corridors"),
    ("International North-South Transport Corridor OR INSTC OR Chabahar port corridor when:24h",
     "Коридори · Північ–Південь (INSTC)", "corridors"),
    ("Global Gateway transport corridor OR TRACECA OR EU Central Asia connectivity OR cross-border railway agreement when:24h",
     "Коридори · Global Gateway і TRACECA", "corridors"),
    ("Belt and Road Initiative railway port logistics OR Lobito Corridor OR transport corridor agreement when:24h",
     "Коридори · BRI, порти та Лобіто", "corridors"),

    # Японія залишається в моніторингу, але не може домінувати у випуску.
    ("Bank of Japan OR BOJ OR yen interest rate policy when:24h",
     "Японія · монетарна політика", "japan"),

    # Глобальні сировинні та енергетичні ринки.
    ("global oil markets OR IEA report OR critical minerals OR copper supply OR LNG when:24h",
     "Глобальні сировинні ринки", "global"),
    ("OPEC+ production OR natural gas prices Europe OR LNG market when:24h",
     "Енергетика та LNG", "global"),
]

FEEDS = []
for query, label, group in search_queries:
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
    FEEDS.append((url, label, True, group))

# Офіційні та міжурядові стрічки на додачу до пошуку публікацій у медіа.
FEEDS.extend([
    ("https://www.imf.org/en/News/RSS", "МВФ", False, "global"),
    ("https://www.wto.org/english/res_e/wto_rss_e.xml", "СОТ", False, "global"),
    ("https://www.bis.org/doclist/all_pressrels.rss", "BIS", False, "global"),
    ("https://www.ecb.europa.eu/rss/press.html", "ЄЦБ", False, "eu"),
    ("https://www.consilium.europa.eu/en/rss/pressreleases.ashx", "Рада ЄС", False, "eu"),
    ("https://search.worldbank.org/api/v2/news?format=atom", "Світовий банк", False, "global"),
])

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
XAI_API_KEY = (os.environ.get("XAI_API_KEY") or "").strip().replace('"', '').replace("'", "")
XAI_MODEL = (os.environ.get("XAI_MODEL") or "grok-4.7").strip()
if XAI_MODEL == "grok-4.3":
    XAI_MODEL = "grok-4.3-latest"

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
        
        # Насамперед моделі з автоматичним prompt caching; далі — резервні моделі.
        def model_priority(model):
            name = model.lower()
            if "gpt-oss-120b" in name:
                return (0, name)
            if "gpt-oss-20b" in name:
                return (1, name)
            if "llama" in name:
                return (2, name)
            if "qwen" in name:
                return (3, name)
            return (4, name)

        valid_models.sort(key=model_priority)
        
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

    # Короткий контекст: не передаємо всі 80 записів, інакше перевищується ліміт Groq.
    recent_block = "(поки що порожньо)"
    if recent_posts:
        recent_block = "\n".join(
            f"- [{p.get('source', 'джерело')}] {(p.get('original_title') or p.get('title', ''))[:140]}: {p.get('summary', '')[:120]}"
            for p in recent_posts[-8:]
        )

    # Статичні інструкції стоять першими, щоб Groq міг повторно використовувати їх кеш.
    prompt = (
        "Ти макроекономічний аналітик. Підготуй точний технічний допис для інвесторів і політиків. "
        "Відповідай лише українською, використовуй тільки підтверджені у статті факти й не вигадуй наслідків.\n"
        "Класифікація: relevant=false лише для нерелевантних дрібних новин. "
        "Познач duplicate=true, якщо стаття повторює вже опубліковану подію без нового суттєвого факту; інший заголовок або джерело самі по собі не роблять її новою. "
        "Окремий ключовий показник того самого звіту залишай як новий матеріал лише якщо він додає конкретні дані, яких немає в контексті, та змінює оцінку ринку або політики. "
        "Спільна тема не означає дубль: нове рішення, стадія події або незалежний ринковий рух із новими цифрами можуть бути окремими новинами. Не повторюй уже наведені факти.\n"
        "Для нової важливої події створи title до 10 слів та analysis одним абзацом українською, 3-4 речення, до 500 символів. "
        "Вкажи факт, ключові цифри/умови й конкретний наслідок для ліквідності, боргу, валют або постачань. Без кліше та загальних фраз. "
        'Поверни лише JSON: {"relevant":true,"duplicate":false,"title":"...","analysis":"..."}\n\n'
        f"Видання/джерело й тематичний напрям: {source_name}\nОригінальний заголовок: {title}\nТекст статті:\n{article_text[:2500]}\n\n"
        "Попередні дописи для перевірки повторів (оригінальний заголовок може бути іншою мовою):\n"
        f"{recent_block}"
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    # Обмежуємо кількість моделей: 429 зазвичай означає спільний ліміт API.
    for model in active_models[:GROQ_MAX_MODELS_TO_TRY]:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 450,
        }

        time.sleep(GROQ_CALL_DELAY)
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)

            if resp.status_code == 429:
                print(
                    f"Groq 429 для {model}; не чекаю retry-after "
                    "і не перебираю інші моделі, щоб не блокувати запуск."
                )
                break

            if resp.status_code != 200:
                print(f"Groq ({model}): HTTP {resp.status_code}: {resp.text[:300]}")
                continue

            data = resp.json()
            usage = data.get("usage") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            print(
                f"Groq usage ({model}): input={usage.get('prompt_tokens', '?')}, "
                f"cached={prompt_details.get('cached_tokens', 0)}, "
                f"output={usage.get('completion_tokens', '?')}"
            )
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


def analyze_with_anthropic(title, article_text, source_name, recent_posts):
    """Резервний макроекономічний аналіз через Anthropic після Groq."""
    if not ANTHROPIC_API_KEY:
        print("Anthropic: відсутній ANTHROPIC_API_KEY; резерв вимкнений.")
        return {"relevant": False, "duplicate": False, "failed": True}

    recent_block = "\n".join(
        f"- [{p.get('source', 'джерело')}] {(p.get('original_title') or p.get('title', ''))[:140]}: {p.get('summary', '')[:120]}"
        for p in (recent_posts or [])[-8:]
    ) or "(поки що порожньо)"
    prompt = (
        "Ти макроекономічний аналітик. Відповідай лише українською, використовуй тільки факти зі статті та не вигадуй даних.\n"
        "Познач duplicate=true, якщо матеріал повторює вже опубліковану подію без нового суттєвого факту; інший заголовок або джерело самі по собі не роблять її новою. "
        "Окремий показник того самого звіту залишай лише якщо він додає конкретні дані, яких немає в контексті, та змінює оцінку ринку або політики. "
        "Спільна тема не означає дубль; окреме рішення або незалежна ринкова реакція з новими цифрами може бути самостійною новиною. "
        "Нерелевантні дрібні новини позначай relevant=false. Для нової важливої події створи title до 10 слів і analysis одним абзацом на 3-4 речення, до 500 символів. "
        "Зазнач факт, цифри/умови та конкретний макрофінансовий наслідок. Поверни лише валідний JSON: "
        '{"relevant":true,"duplicate":false,"title":"...","analysis":"..."}\n\n'
        f"Видання/джерело й тематичний напрям: {source_name}\nОригінальний заголовок: {title}\nТекст статті:\n{article_text[:2500]}\n\n"
        f"Останні дописи для перевірки дублікатів:\n{recent_block}"
    )
    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 600,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=(5, 35),
        )
        if resp.status_code != 200:
            print(f"Anthropic ({ANTHROPIC_MODEL}): HTTP {resp.status_code}: {resp.text[:500]}")
            return {"relevant": False, "duplicate": False, "failed": True}
        blocks = resp.json().get("content", [])
        content = "\n".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        match = re.search(r"\{.*\}", content, re.DOTALL)
        parsed = json.loads(match.group(0) if match else content)
        ai_title = clean_text(parsed.get("title", ""))
        ai_analysis = clean_text(parsed.get("analysis", ""))

        if not ai_title or len(ai_title) > 180 or len(ai_analysis) < 20:
            raise ValueError("Anthropic повернув неповний заголовок або аналіз")

        print(f"Anthropic ({ANTHROPIC_MODEL}) успішно обробив новину.")
        return {
            "relevant": bool(parsed.get("relevant", True)),
            "duplicate": bool(parsed.get("duplicate", False)),
            "title": ai_title,
            "analysis": ai_analysis,
            "failed": False,
        }
    except Exception as e:
        print(f"Anthropic ({ANTHROPIC_MODEL}): помилка аналізу: {e}")
        return {"relevant": False, "duplicate": False, "failed": True}

def analyze_with_xai(title, article_text, source_name, recent_posts):
    """Резервний аналіз через платний xAI API, якщо Groq недоступний."""
    if not XAI_API_KEY:
        print("xAI: відсутній XAI_API_KEY; резервний аналіз неможливий.")
        return {"relevant": False, "duplicate": False, "failed": True}

    recent_block = "\n".join(
        f"- [{p.get('source', 'джерело')}] {(p.get('original_title') or p.get('title', ''))[:140]}: {p.get('summary', '')[:120]}"
        for p in (recent_posts or [])[-8:]
    ) or "(поки що порожньо)"
    prompt = (
        "Ти макроекономічний аналітик. Відповідай лише українською, використовуй тільки факти зі статті та не вигадуй даних.\n"
        "Познач duplicate=true, якщо матеріал повторює вже опубліковану подію без нового суттєвого факту; інший заголовок або джерело самі по собі не роблять її новою. "
        "Окремий показник того самого звіту залишай лише якщо він додає конкретні дані, яких немає в контексті, та змінює оцінку ринку або політики. "
        "Спільна тема не означає дубль; окреме рішення або незалежна ринкова реакція з новими цифрами може бути самостійною новиною. "
        "Нерелевантні дрібні новини позначай relevant=false. Для нової важливої події створи title до 10 слів і analysis одним абзацом на 3-4 речення, до 500 символів. "
        "Зазнач факт, цифри/умови та конкретний макрофінансовий наслідок. Поверни лише JSON: "
        '{"relevant":true,"duplicate":false,"title":"...","analysis":"..."}\n\n'
        f"Видання/джерело й тематичний напрям: {source_name}\nОригінальний заголовок: {title}\nТекст статті:\n{article_text[:2500]}\n\n"
        f"Останні дописи для перевірки дублікатів:\n{recent_block}"
    )
    try:
        resp = requests.post(
            XAI_API_URL,
            headers={"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": XAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "max_tokens": 450,
            },
            timeout=(5, 35),
        )
        if resp.status_code != 200:
            print(f"xAI ({XAI_MODEL}): HTTP {resp.status_code}: {resp.text[:500]}")
            return {"relevant": False, "duplicate": False, "failed": True}
        content = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r"\{.*\}", content, re.DOTALL)
        parsed = json.loads(match.group(0) if match else content)
        ai_title = parsed.get("title", "")
        ai_analysis = parsed.get("analysis", "")
        if len(ai_title.strip()) < 5 or len(ai_analysis.strip()) < 20:
            raise ValueError("xAI повернув неповний заголовок або аналіз")
        return {
            "relevant": bool(parsed.get("relevant", True)),
            "duplicate": bool(parsed.get("duplicate", False)),
            "title": ai_title,
            "analysis": ai_analysis,
            "failed": False,
        }
    except Exception as e:
        print(f"xAI ({XAI_MODEL}): помилка аналізу: {e}")
        return {"relevant": False, "duplicate": False, "failed": True}

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

    for feed_url, source_name, is_topic_query, source_group in FEEDS:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=8)
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

            rss_source = entry.get("source", {})
            publisher = rss_source.get("title", "") if hasattr(rss_source, "get") else ""
            if not is_topic_query:
                publisher = source_name
            elif not publisher:
                publisher = "Google News"

            all_entries.append({
                "link": link.strip(),
                "title": entry.get("title", "Без заголовка"),
                "summary": strip_html(entry.get("summary", ""))[:1500],
                "source": source_name,
                "publisher": publisher,
                "is_topic_query": is_topic_query,
                "group": source_group,
                "published": published_dt,
            })

    all_entries.sort(key=lambda e: e["published"], reverse=True)

    # Одна й та сама стаття може прийти з кількох пошукових запитів.
    unique_entries = []
    seen_links = set()
    for entry in all_entries:
        normalized_link = canonical_link(entry["link"]) or entry["link"]
        if normalized_link in seen_links:
            continue
        seen_links.add(normalized_link)
        unique_entries.append(entry)

    # Round-robin за напрямами: найсвіжіший запис кожної групи перед другим записом
    # будь-якої групи. Це зменшує перевагу японської монетарної тематики та енергетики.
    grouped = {}
    for entry in unique_entries:
        grouped.setdefault(entry.get("group", "global"), []).append(entry)

    group_order = GROUP_ORDER + [
        group for group in grouped if group not in GROUP_ORDER
    ]
    balanced_entries = []
    max_group_size = max((len(items) for items in grouped.values()), default=0)
    for index in range(max_group_size):
        for group in group_order:
            items = grouped.get(group, [])
            if index < len(items):
                balanced_entries.append(items[index])

    distribution = {group: len(items) for group, items in grouped.items()}
    print(f"Унікальних свіжих записів: {len(unique_entries)}.")
    print(f"Кандидати за напрямами: {distribution}.")
    return balanced_entries

def format_message(entry, ai_title, ai_analysis):
    raw_title = ai_title or entry["title"]
    clean_title = clean_text(raw_title)
    safe_title = html.escape(clean_title)

    date_str = entry["published"].strftime("%d.%m.%Y")
    publisher = entry.get("publisher") or entry.get("source") or "Невідоме видання"
    safe_publisher = html.escape(publisher)
    safe_topic = html.escape(entry.get("source", ""))

    parts = [
        f"<b>{safe_title}</b>",
        "",
        f"🗓 {date_str} | 📰 {safe_publisher}",
    ]
    if entry.get("is_topic_query") and safe_topic:
        parts.append(f"🧭 Тематичний напрям: {safe_topic}")

    if ai_analysis and len(ai_analysis.strip()) > 5:
        clean_analysis = clean_text(ai_analysis)
        safe_analysis = html.escape(clean_analysis)
        parts += ["", f"{safe_analysis}"]

    safe_link = entry["link"].replace('"', "%22")
    parts += ["", f'<a href="{safe_link}">Читати першоджерело</a>']
    return "\n".join(parts)


def main():
    history = load_history()
    shared_history = load_shared_history()
    combined_recent_posts = (history["recent_posts"] + shared_history["recent_posts"])[-80:]
    entries = collect_entries()
    print(f"Зібрано свіжих записів: {len(entries)}")

    active_models = get_available_groq_models()
    if not active_models:
        print("Groq недоступний; спершу буде використано Anthropic, далі xAI.")

    new_posts = 0
    checked = 0
    checked_by_group = {}
    posted_by_group = {}

    for entry in entries:
        if new_posts >= MAX_POSTS_PER_RUN or checked >= MAX_ENTRIES_CHECKED_PER_RUN:
            break

        group = entry.get("group", "global")
        if posted_by_group.get(group, 0) >= MAX_POSTS_PER_GROUP.get(group, 2):
            continue

        if entry["link"] in history["links"]:
            continue
        if is_duplicate_link(entry["link"], shared_history):
            print("Пропущено: посилання вже є в спільній історії.")
            history["links"].append(entry["link"])
            continue
        if is_duplicate_title(entry["title"], shared_history["recent_posts"]):
            print("Пропущено: заголовок схожий на нещодавню публікацію.")
            history["links"].append(entry["link"])
            continue

        if checked_by_group.get(group, 0) >= MAX_CHECKED_PER_GROUP.get(group, 3):
            continue
        checked += 1
        checked_by_group[group] = checked_by_group.get(group, 0) + 1

        article_text = fetch_article_text(entry["link"])
        if not article_text:
            article_text = entry["summary"]

        # Select likely event matches from the live shared history.
        duplicate_context = select_relevant_recent_posts(
            entry["title"], article_text, shared_history["recent_posts"], limit=8
        )
        source_context = entry.get("publisher") or entry["source"]
        if entry.get("is_topic_query"):
            source_context = f"{source_context}; тематичний напрям: {entry['source']}"
        result = analyze_with_groq(
            entry["title"], article_text, source_context, duplicate_context, active_models
        )

        if result.get("failed"):
            print(f"Groq не обробив новину; пробую Anthropic: {entry['title']}")
            result = analyze_with_anthropic(
                entry["title"], article_text, source_context, duplicate_context
            )

        if result.get("failed"):
            print(f"Anthropic не обробив новину; пробую xAI: {entry['title']}")
            result = analyze_with_xai(
                entry["title"], article_text, source_context, duplicate_context
            )

        if result.get("failed"):
            print(f"Пропущено тимчасово (Groq, Anthropic і xAI не змогли обробити новину): {entry['title']}")
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
                "original_title": entry["title"],
                "summary": (result["analysis"] or entry["summary"])[:300],
                "source": entry["source"],
                "publisher": entry.get("publisher", ""),
            })
            record_published(
                shared_history, entry["link"],
                result["title"] or entry["title"], result["analysis"] or entry["summary"],
                entry["source"], "economy",
                entry["published"].isoformat() if hasattr(entry["published"], "isoformat") else "",
                publisher=entry.get("publisher", ""),
                original_title=entry["title"],
            )
            new_posts += 1
            posted_by_group[group] = posted_by_group.get(group, 0) + 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(3)
        else:
            print(f"Помилка публікації: {entry['title']}")

    save_history(history)
    save_shared_history(shared_history)
    print(f"Перевірено за напрямами: {checked_by_group}; опубліковано за напрямами: {posted_by_group}.")
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
