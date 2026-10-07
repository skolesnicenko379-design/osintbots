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
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ADMIN_ID = os.environ.get("ADMIN_ID")

HISTORY_FILE = "posted_news.json"
MAX_POSTS_PER_RUN = 6
MAX_ENTRIES_CHECKED_PER_RUN = 50
RECENT_POSTS_FOR_DEDUP = 20
MAX_ARTICLE_AGE_HOURS = 24  # Тільки свіжі матеріали за останні 24 години
REQUEST_TIMEOUT = 20
ARTICLE_FETCH_TIMEOUT = 20
ARTICLE_MAX_CHARS = 4000
ANTHROPIC_TIMEOUT = 45          # пауза очікування відповіді моделі
ANTHROPIC_MAX_RETRIES = 2
ANTHROPIC_RETRY_DELAY = 4       # базова пауза між спробами (секунди)
ANTHROPIC_CALL_DELAY = 1.5      # пауза ПЕРЕД кожним викликом API, щоб не впертися в rate limit

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

ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
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
