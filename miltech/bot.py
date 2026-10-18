import os
import json
import time
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import re
import requests

import sys
sys.path.insert(0, os.path.abspath(".."))
from shared_dedup import (
    load_shared_history, save_shared_history, is_duplicate_link,
    is_duplicate_title, select_relevant_recent_posts, record_published,
)
import feedparser
from bs4 import BeautifulSoup

# ===== Налаштування з GitHub Secrets =====
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip().replace('"', '').replace("'", "")
CHANNEL_ID = (os.environ.get("CHANNEL_ID") or "").strip()
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or "").strip().replace('"', '').replace("'", "")
ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip().replace('"', '').replace("'", "")
ANTHROPIC_MODEL = (os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-5-5").strip()
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 4
MAX_AI_CANDIDATES_PER_RUN = 8
REQUEST_TIMEOUT = 12
FEED_CONNECT_TIMEOUT = 4
FEED_READ_TIMEOUT = 8
ARTICLE_FETCH_TIMEOUT = 8
GROQ_TIMEOUT = 15

# Джерела: (URL, Назва джерела, чи потрібен переклад)
FEEDS = [
    ("https://mil.in.ua/uk/news/feed/", "mil.in.ua", False),
    ("https://defence-ua.com/rss.xml", "defence-ua.com", False),
    ("https://breakingdefense.com/feed/", "Breaking Defense", True),

    # Активні англомовні джерела: новини оборонної промисловості, озброєнь і військових технологій
    ("https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", "Defense News", True),
    ("https://www.twz.com/feed", "The War Zone", True),

    # Джерела Google Alerts із кастомними назвами для Telegram
    ("https://www.google.com/alerts/feeds/12089626364797798521/7402252502089930204", "Western Defense Industry", True),
    ("https://www.google.com/alerts/feeds/12089626364797798521/17810137244338497811", "Global MilTech", True),
]

# АКТУАЛЬНІ РОБОЧІ МОДЕЛІ GROQ
# ВАЖЛИВО: "llama3-70b-8192", "mixtral-8x7b-32768" і "llama3-8b-8192" офіційно
# зняті з підтримки (model_decommissioned). Використовуємо моделі, які реально
# доступні на звичайному (developer) Groq API-ключі станом на зараз.
# Якщо колись знову отримаєте помилку model_not_found / model_decommissioned,
# перевірте актуальний список командою:
#   curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"
GROQ_MODELS = [
    "openai/gpt-oss-120b",    # Найкраща для аналітики
    "openai/gpt-oss-20b",     # Швидка запасна
]
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
XAI_API_KEY = (os.environ.get("XAI_API_KEY") or "").strip().replace('"', '').replace("'", "")
XAI_MODEL = (os.environ.get("XAI_MODEL") or "grok-4.7").strip()
if XAI_MODEL == "grok-4.3":
    XAI_MODEL = "grok-4.3-latest"

ARTICLE_MAX_CHARS = 3000  # скільки символів тексту статті передавати в Groq


def strip_html(raw):
    return re.sub(r"\s+", " ", BeautifulSoup(raw or "", "html.parser").get_text()).strip()


def fetch_article_text(url):
    """Намагається витягти повний текст статті зі сторінки. Повертає '' при невдачі."""
    try:
        resp = requests.get(
            url,
            timeout=ARTICLE_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
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
def enrich_with_groq(title, article_text, source_name, needs_translation, recent_posts=None):
    """Повертає заголовок, коментар і ознаку міжканального дубліката."""
    if not GROQ_API_KEY:
        return None, None, False

    lang_note = (
        "Оригінал англійською — переклади заголовок природною українською."
        if needs_translation
        else "Оригінал вже українською."
    )

    # Більший контекст допомагає ловити повтори, які повертаються з іншим заголовком
    # або з іншого джерела. Короткий опис обмежує витрати токенів.
    recent_block = "\n".join(
        f"- [{post.get('channel', 'інший канал')}] {post.get('title', '')}: {post.get('summary', '')[:100]}"
        for post in (recent_posts or [])[-15:]
    ) or "(нещодавніх публікацій немає)"

    prompt = (
        "Ти — редактор мілтех-новин для українського Telegram-каналу.\n"
        f"Джерело: {source_name}. {lang_note}\n\n"
        f"Оригінальний заголовок: {title}\n\n"
        f"Повний текст новини:\n{article_text}\n\n"
        f"ОСТАННІ ПУБЛІКАЦІЇ В УСІХ КАНАЛАХ:\n{recent_block}\n\n"
        "Перевір, чи описує матеріал ту саму подію, що й одна з попередніх публікацій. "
        "Інше джерело або перефразований заголовок не роблять подію новою. Якщо є суттєвий новий факт, "
        "окремий етап або нове рішення, duplicate=false. Інакше duplicate=true.\n"
        "Виконай ДВІ речі на основі ЗМІСТУ новини:\n"
        "1) Дай стислий, точний заголовок українською (до 15 слів), без клікбейту.\n"
        "2) Дай 2-3 речення технічного/аналітичного коментаря українською: що саме сталося, "
        "які характеристики техніки згадуються, який можливий військовий вплив.\n"
        "КРИТИЧНО: Поле 'analysis' НІКОЛИ не повинно бути порожнім. Навіть якщо тексту мало, придумай короткий контекст.\n\n"
        "Відповідай СТРОГО у форматі JSON без жодного іншого тексту:\n"
        '{"duplicate": false, "title": "...", "analysis": "..."}'
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    # Перебираємо моделі, щоб не падати при відключенні однієї
    for model in GROQ_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},  # Гарантує повернення JSON
        }

        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=GROQ_TIMEOUT)

            if resp.status_code == 404 or (resp.status_code == 400 and ("does not exist" in resp.text or "model_decommissioned" in resp.text)):
                print(f"Groq: модель {model} недоступна, пробую наступну...")
                continue

            if resp.status_code == 429:
                retry_after = 5
                try:
                    retry_after = min(float(resp.headers.get("retry-after", 3)), 3)
                except Exception:
                    pass
                time.sleep(retry_after)
                continue

            if resp.status_code != 200:
                print(f"Groq: HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(1)
                continue

            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()

            parsed = json.loads(text)

            ai_title = parsed.get("title")
            ai_analysis = parsed.get("analysis")

            # Якщо ШІ все ж повернув порожньо, даємо базовий текст
            if not ai_analysis or len(ai_analysis.strip()) < 5:
                ai_analysis = "Додаткові технічні деталі уточнюються."

            return ai_title, ai_analysis, parsed.get("duplicate", False) is True

        except Exception as e:
            print(f"Groq ({model}): помилка ({e})")
            time.sleep(3)
            continue

    return None, None, False


# ===== Telegram =====


def enrich_with_anthropic(title, article_text, source_name, needs_translation, recent_posts=None):
    """Резервний переклад та аналіз через Anthropic Claude API."""
    if not ANTHROPIC_API_KEY:
        print("Anthropic: відсутній ANTHROPIC_API_KEY; резерв вимкнений.")
        return None, None, False

    lang_note = (
        "Оригінал англійською — переклади заголовок природною українською."
        if needs_translation else "Оригінал вже українською."
    )
    recent_block = "\n".join(
        f"- [{post.get('channel', 'інший канал')}] {post.get('title', '')}: {post.get('summary', '')[:100]}"
        for post in (recent_posts or [])[-12:]
    ) or "(нещодавніх публікацій немає)"
    prompt = (
        "Ти — редактор мілтех-новин для українського Telegram-каналу.\n"
        f"Джерело: {source_name}. {lang_note}\n"
        f"Оригінальний заголовок: {title}\n\n"
        f"Текст новини: {article_text[:3000]}\n\n"
        f"Останні публікації для перевірки повторів:\n{recent_block}\n\n"
        "Перевір, чи це та сама подія, що й у попередніх публікаціях. Інше джерело або перефразований "
        "заголовок не роблять подію новою. Якщо є суттєвий новий факт, duplicate=false. "
        "Створи точний заголовок українською до 15 слів і 2-3 речення технічного коментаря про подію, "
        "характеристики техніки та можливий військовий вплив. Не вигадуй характеристик чи фактів. "
        "analysis не може бути порожнім. Поверни лише валідний JSON: "
        '{"duplicate":false,"title":"...","analysis":"..."}'
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
                "max_tokens": 700,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=(5, 35),
        )
        if resp.status_code != 200:
            print(f"Anthropic ({ANTHROPIC_MODEL}): HTTP {resp.status_code}: {resp.text[:500]}")
            return None, None, False
        blocks = resp.json().get("content", [])
        content = "\n".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        match = re.search(r'\{.*\}', content, re.DOTALL)
        parsed = json.loads(match.group(0) if match else content)
        ai_title = parsed.get("title")
        ai_analysis = parsed.get("analysis")
        if not ai_title or len(ai_title.strip()) < 5 or not ai_analysis or len(ai_analysis.strip()) < 20:
            raise ValueError("Anthropic повернув неповний заголовок або аналіз")
        print(f"Anthropic ({ANTHROPIC_MODEL}) успішно обробив новину.")
        return ai_title, ai_analysis, parsed.get("duplicate", False) is True
    except Exception as e:
        print(f"Anthropic ({ANTHROPIC_MODEL}): помилка аналізу: {e}")
        return None, None, False


def enrich_with_xai(title, article_text, source_name, needs_translation, recent_posts=None):
    """Резервний переклад та аналіз через платний xAI API."""
    if not XAI_API_KEY:
        print("xAI: відсутній XAI_API_KEY; резервний аналіз неможливий.")
        return None, None, False

    lang_note = (
        "Оригінал англійською — переклади заголовок природною українською."
        if needs_translation else "Оригінал вже українською."
    )
    recent_block = "\n".join(
        f"- [{post.get('channel', 'інший канал')}] {post.get('title', '')}: {post.get('summary', '')[:100]}"
        for post in (recent_posts or [])[-12:]
    ) or "(нещодавніх публікацій немає)"
    prompt = (
        "Ти — редактор мілтех-новин для українського Telegram-каналу.\\n"
        f"Джерело: {source_name}. {lang_note}\\n"
        f"Оригінальний заголовок: {title}\\n\\n"
        f"Текст новини: {article_text[:3000]}\\n\\n"
        f"Останні публікації для перевірки повторів:\\n{recent_block}\\n\\n"
        "Перевір, чи це та сама подія, що й у попередніх публікаціях. Інше джерело або перефразований "
        "заголовок не роблять подію новою. Якщо є суттєвий новий факт, duplicate=false. "
        "Створи точний заголовок українською до 15 слів і 2-3 речення технічного коментаря про подію, "
        "характеристики техніки та можливий військовий вплив. Не вигадуй характеристик чи фактів. "
        "analysis не може бути порожнім. Поверни лише JSON: "
        '{"duplicate":false,"title":"...","analysis":"..."}'
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
                "max_tokens": 700,
            },
            timeout=(5, 35),
        )
        if resp.status_code != 200:
            print(f"xAI ({XAI_MODEL}): HTTP {resp.status_code}: {resp.text[:500]}")
            return None, None, False
        content = resp.json()["choices"][0]["message"]["content"].strip()
        match = re.search(r'\{.*\}', content, re.DOTALL)
        parsed = json.loads(match.group(0) if match else content)
        ai_title = parsed.get("title")
        ai_analysis = parsed.get("analysis")
        if not ai_title or len(ai_title.strip()) < 5 or not ai_analysis or len(ai_analysis.strip()) < 20:
            raise ValueError("xAI повернув неповний заголовок або аналіз")
        return ai_title, ai_analysis, parsed.get("duplicate", False) is True
    except Exception as e:
        print(f"xAI ({XAI_MODEL}): помилка аналізу: {e}")
        return None, None, False

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
def fetch_feed(feed_config):
    feed_url, source_name, needs_translation = feed_config
    started = time.monotonic()
    try:
        response = requests.get(
            feed_url,
            timeout=(FEED_CONNECT_TIMEOUT, FEED_READ_TIMEOUT),
            headers={"User-Agent": "Mozilla/5.0 (compatible; OSINTNewsBot/1.0)"},
        )
        print(f"[RSS] {source_name}: HTTP {response.status_code}")
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception as e:
        print(f"[RSS] {source_name}: ERROR after {time.monotonic() - started:.1f}s: {e}")
        return []
    if getattr(feed, "bozo", False):
        print(f"[RSS] {source_name}: feed parse warning: {getattr(feed, 'bozo_exception', 'unknown error')}")
    print(f"[RSS] {source_name}: fetched_entries={len(feed.entries)}, runtime={time.monotonic() - started:.1f}s")
    entries = []
    for entry in feed.entries[:5]:
        link = entry.get("link")
        if not link:
            continue
        published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if published_struct:
            published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
        else:
            published_dt = datetime.now(timezone.utc)
        entries.append({
            "link": link,
            "title": entry.get("title", "Без заголовка"),
            "summary": strip_html(entry.get("summary", ""))[:1200],
            "source": source_name,
            "needs_translation": needs_translation,
            "published": published_dt,
        })
    return entries


def collect_entries():
    all_entries = []
    with ThreadPoolExecutor(max_workers=min(5, len(FEEDS))) as pool:
        futures = [pool.submit(fetch_feed, config) for config in FEEDS]
        for future in as_completed(futures):
            try:
                all_entries.extend(future.result())
            except Exception as e:
                print(f"Помилка обробки RSS: {e}")
    all_entries.sort(key=lambda e: e["published"], reverse=True)
    counts = {}
    for entry in all_entries:
        counts[entry["source"]] = counts.get(entry["source"], 0) + 1
    print(f"[RSS] collection complete: total_entries={len(all_entries)}; by_source={counts}")
    return all_entries


def format_message(entry, groq_title, groq_analysis):
    title = groq_title or entry["title"]
    date_str = entry["published"].strftime("%d.%m.%Y")

    parts = [
        f"<b>{html.escape(title)}</b>",
        "",
        f"🗓 {date_str} | 📡 {html.escape(entry['source'])}",
    ]

    # Малюємо 🔎 тільки якщо аналіз дійсно існує
    if groq_analysis and len(groq_analysis.strip()) > 3:
        parts += ["", f"🔎 {html.escape(groq_analysis)}"]

    parts += ["", f"<a href='{html.escape(entry['link'])}'>Читати першоджерело</a>"]
    return "\n".join(parts)


def main():
    run_started = time.monotonic()
    history = load_history()
    shared_history = load_shared_history()

    stage_started = time.monotonic()
    entries = collect_entries()
    print(f"[TIME] collect_entries: {time.monotonic() - stage_started:.1f}s")

    # Спочатку прибираємо відомі повтори, окремо рахуємо причини відсіву.
    candidates = []
    skipped = {"local_history": 0, "shared_link": 0, "shared_title": 0}
    for entry in entries:
        if entry["link"] in history:
            skipped["local_history"] += 1
            continue
        if is_duplicate_link(entry["link"], shared_history):
            skipped["shared_link"] += 1
            print(f"[FILTER] shared-link duplicate: source={entry['source']} title={entry['title'][:100]}")
            history.append(entry["link"])
            continue
        if is_duplicate_title(entry["title"], shared_history["recent_posts"]):
            skipped["shared_title"] += 1
            print(f"[FILTER] shared-title duplicate: source={entry['source']} title={entry['title'][:100]}")
            history.append(entry["link"])
            continue
        candidates.append(entry)

    candidate_counts = {}
    for item in candidates:
        candidate_counts[item["source"]] = candidate_counts.get(item["source"], 0) + 1
    print(f"[FILTER] unseen_candidates={len(candidates)}; skipped={skipped}; by_source={candidate_counts}")

    candidate_count_before_cap = len(candidates)
    if candidate_count_before_cap > MAX_AI_CANDIDATES_PER_RUN:
        # Резервуємо до двох місць для Google Alerts: по одному на кожну стрічку.
        # Решту місць заповнюємо найсвіжішими новинами з усіх джерел.
        alerts_sources = {"Western Defense Industry", "Global MilTech"}
        reserved = []
        reserved_links = set()
        for source in alerts_sources:
            match = next((item for item in candidates if item["source"] == source), None)
            if match:
                reserved.append(match)
                reserved_links.add(match["link"])
        remaining_slots = max(0, MAX_AI_CANDIDATES_PER_RUN - len(reserved))
        remaining = [item for item in candidates if item["link"] not in reserved_links]
        selected = sorted(remaining, key=lambda item: item["published"], reverse=True)[:remaining_slots]
        candidates = sorted(reserved + selected, key=lambda item: item["published"], reverse=True)
        selected_counts = {}
        for item in candidates:
            selected_counts[item["source"]] = selected_counts.get(item["source"], 0) + 1
        print(f"[FILTER] candidate_cap={MAX_AI_CANDIDATES_PER_RUN}; alerts_reserved={[item['source'] for item in reserved]}; selected_by_source={selected_counts}; excluded_by_cap={candidate_count_before_cap - len(candidates)}")

    # Завантажуємо обрані статті паралельно, але виклики Groq залишаємо послідовними.
    stage_started = time.monotonic()
    article_texts = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        future_map = {pool.submit(fetch_article_text, entry["link"]): entry for entry in candidates}
        for future in as_completed(future_map):
            entry = future_map[future]
            try:
                article_texts[entry["link"]] = future.result()
            except Exception as e:
                print(f"Помилка завантаження статті {entry['link']}: {e}")
                article_texts[entry["link"]] = ""
    print(f"[TIME] article downloads ({len(candidates)} candidates): {time.monotonic() - stage_started:.1f}s")

    new_posts = 0
    for index, entry in enumerate(candidates, start=1):
        if new_posts >= MAX_POSTS_PER_RUN:
            break

        article_text = article_texts.get(entry["link"], "")
        if not article_text or len(article_text) < 50:
            article_text = entry["summary"]

        stage_started = time.monotonic()
        duplicate_context = select_relevant_recent_posts(
            entry["title"], article_text, shared_history["recent_posts"], limit=15
        )
        groq_title, groq_analysis, is_duplicate = enrich_with_groq(
            entry["title"], article_text, entry["source"],
            entry["needs_translation"], duplicate_context,
        )
        if not groq_title or not groq_analysis:
            print(f"Groq не обробив новину; пробую Anthropic: {entry['title']}")
            groq_title, groq_analysis, is_duplicate = enrich_with_anthropic(
                entry["title"], article_text, entry["source"],
                entry["needs_translation"], duplicate_context,
            )
        if not groq_title or not groq_analysis:
            print(f"Anthropic не обробив новину; пробую xAI: {entry['title']}")
            groq_title, groq_analysis, is_duplicate = enrich_with_xai(
                entry["title"], article_text, entry["source"],
                entry["needs_translation"], duplicate_context,
            )
        print(f"[TIME] AI candidate {index}/{len(candidates)}: {time.monotonic() - stage_started:.1f}s")

        if not groq_title or not groq_analysis:
            print("Пропущено тимчасово: Groq, Anthropic і xAI не змогли перевірити новину; її можна повторно обробити наступним запуском.")
            continue
        if is_duplicate:
            history.append(entry["link"])
            print("Пропущено моделлю: подія вже висвітлена в іншому каналі.")
            continue

        message = format_message(entry, groq_title, groq_analysis)
        stage_started = time.monotonic()
        if send_to_telegram(message):
            history.append(entry["link"])
            record_published(
                shared_history, entry["link"], groq_title or entry["title"],
                groq_analysis or entry["summary"], entry["source"], "miltech",
                entry["published"].isoformat() if hasattr(entry["published"], "isoformat") else "",
                original_title=entry["title"],
            )
            new_posts += 1
            print(f"Опубліковано: {entry['title']}")
            time.sleep(1)
        else:
            print(f"Не вдалося опублікувати: {entry['title']}")
        print(f"[TIME] Telegram send: {time.monotonic() - stage_started:.1f}s")

    save_history(history)
    save_shared_history(shared_history)
    print(f"[TIME] TOTAL runtime: {time.monotonic() - run_started:.1f}s")
    print(f"Готово. Опубліковано новин: {new_posts}")


if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        main()
    else:
        print("Помилка: не знайдені TELEGRAM_TOKEN або CHANNEL_ID")
