"""Спільний контроль дублікатів для трьох Telegram-ботів."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

ROOT = Path(__file__).resolve().parent
SHARED_HISTORY_FILE = ROOT / "shared_news_history.json"
HISTORY_WINDOW_HOURS = 42
LEGACY_FILES = {
    "diplomacy": ROOT / "diplomacy" / "posted_news.json",
    "economy": ROOT / "economy" / "posted_news.json",
    "miltech": ROOT / "miltech" / "posted_news.json",
}


def canonical_link(link):
    """Нормалізує посилання для точного зіставлення."""
    link = str(link or "").strip()
    if not link:
        return ""
    try:
        parsed = urlparse(link)
        host = parsed.netloc.lower().removeprefix("www.")
        query = parse_qs(parsed.query)
        if host == "google.com" and parsed.path == "/url":
            wrapped = (query.get("url") or query.get("q") or [""])[0]
            if wrapped:
                return canonical_link(wrapped)
        if host == "news.google.com" and parsed.path.startswith("/rss/articles/"):
            return urlunparse((parsed.scheme or "https", host, parsed.path, "", "", ""))
        clean_query = {
            key: values for key, values in query.items()
            if not key.lower().startswith("utm_")
            and key.lower() not in {"fbclid", "gclid", "oc", "ved", "usg"}
        }
        query_string = "&".join(
            f"{key}={value}"
            for key in sorted(clean_query)
            for value in clean_query[key]
        )
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme.lower() or "https", host, path, "", query_string, ""))
    except Exception:
        return link


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def _normalize_post(post, channel):
    if not isinstance(post, dict):
        return None
    title = str(post.get("title") or "").strip()
    summary = str(post.get("summary") or post.get("analysis") or "").strip()
    if not title:
        return None
    return {
        "title": title[:250],
        "summary": summary[:600],
        "source": str(post.get("source") or "")[:100],
        "channel": str(post.get("channel") or channel),
        "link": canonical_link(post.get("link") or ""),
        "published": str(post.get("published") or ""),
    }


def _published_datetime(value):
    """Розбирає дату публікації в часову мітку UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _within_history_window(post, now=None):
    """Залишає лише записи з підтвердженою датою в межах останніх 42 годин."""
    published = _published_datetime(post.get("published"))
    if published is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - timedelta(hours=HISTORY_WINDOW_HOURS) <= published <= now + timedelta(minutes=5)


def _clean_recent_posts(posts, now=None):
    cleaned = []
    seen = set()
    for post in posts:
        if not isinstance(post, dict):
            continue
        normalized = _normalize_post(post, str(post.get("channel") or "unknown"))
        if not normalized or not _within_history_window(normalized, now):
            continue
        key = (normalized["channel"], normalized["title"].casefold())
        if key in seen:
            continue
        cleaned.append(normalized)
        seen.add(key)
    cleaned.sort(key=lambda post: _published_datetime(post.get("published")) or datetime.min.replace(tzinfo=timezone.utc))
    return cleaned[-150:]


def load_shared_history():
    """Завантажує ковзну історію за 42 години; старі URL не накопичуються безстроково."""
    data = _read_json(SHARED_HISTORY_FILE)
    posts = data.get("recent_posts", []) if isinstance(data, dict) else []
    if not isinstance(posts, list):
        posts = []
    else:
        posts = list(posts)

    # Імпортуємо лише недавні записи з датами. Старі списки URL без дат
    # не можна коректно обрізати за часом, тому вони не переносяться в новий індекс.
    for channel, path in LEGACY_FILES.items():
        legacy = _read_json(path)
        if isinstance(legacy, dict) and isinstance(legacy.get("recent_posts"), list):
            for post in legacy["recent_posts"]:
                normalized = _normalize_post(post, channel)
                if normalized:
                    posts.append(normalized)

    recent_posts = _clean_recent_posts(posts)
    known_links = {post["link"] for post in recent_posts if post.get("link")}
    return {"links": sorted(known_links), "recent_posts": recent_posts}


def save_shared_history(history):
    """Зберігає лише актуальні 42 години та відновлює links із цих записів."""
    recent_posts = _clean_recent_posts(history.get("recent_posts", []))
    history["recent_posts"] = recent_posts
    history["links"] = sorted({
        canonical_link(post.get("link", ""))
        for post in recent_posts
        if canonical_link(post.get("link", ""))
    })
    temp_path = SHARED_HISTORY_FILE.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(history, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, SHARED_HISTORY_FILE)


def is_duplicate_link(link, history):
    normalized = canonical_link(link)
    return bool(normalized) and normalized in {
        canonical_link(item) for item in history.get("links", [])
    }


def _title_tokens(title):
    return {
        token for token in re.findall(r"[a-zа-яіїєґ0-9]+", str(title or "").casefold())
        if len(token) > 2
    }


def is_duplicate_title(title, recent_posts, threshold=0.88):
    """Відсіює майже однакові заголовки; семантичну перевірку лишає моделі."""
    candidate = re.sub(r"\s+", " ", str(title or "").casefold()).strip()
    if len(candidate) < 18:
        return False
    candidate_tokens = _title_tokens(candidate)
    for post in recent_posts or []:
        previous = re.sub(r"\s+", " ", str(post.get("title") or "").casefold()).strip()
        if not previous or len(previous) < 18:
            continue
        if candidate == previous or SequenceMatcher(None, candidate, previous).ratio() >= threshold:
            return True
        previous_tokens = _title_tokens(previous)
        if len(candidate_tokens) >= 4 and len(previous_tokens) >= 4:
            overlap = len(candidate_tokens & previous_tokens) / min(len(candidate_tokens), len(previous_tokens))
            if overlap >= 0.9:
                return True
    return False


def record_published(history, link, title, summary, source, channel, published=""):
    normalized_link = canonical_link(link)
    if normalized_link:
        history.setdefault("links", []).append(normalized_link)
    history.setdefault("recent_posts", []).append({
        "link": normalized_link,
        "title": str(title or "")[:250],
        "summary": str(summary or "")[:600],
        "source": str(source or "")[:100],
        "channel": channel,
        "published": str(published or ""),
    })
    save_shared_history(history)
