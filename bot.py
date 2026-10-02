import os
import json
import time
import requests
import feedparser

# Отримуємо ключі з налаштувань GitHub
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
HISTORY_FILE = "posted_news.json"

# Джерела мілтех-новин (можете додавати свої)
FEEDS = [
    "https://mil.in.ua/uk/news/feed/",
    "https://defence-ua.com/rss.xml",
    "https://breakingdefense.com/feed/"
]

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_history(history):
    # Зберігаємо лише останні 200 новин, щоб файл не розростався
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-200:], f, ensure_ascii=False, indent=4)

def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    response = requests.post(url, json=payload)
    return response.status_code == 200

def main():
    history = load_history()
    new_posts = 0

    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        
        # Перевіряємо перші 3 новини з кожного джерела
        for entry in feed.entries[:3]:
            link = entry.link
            
            if link not in history:
                title = entry.title
                
                # Формування посту. Заголовок жирним, нижче — посилання.
                message = f"<b>{title}</b>\n\n<a href='{link}'>Читати першоджерело</a>"
                
                if send_to_telegram(message):
                    history.append(link)
                    new_posts += 1
                    print(f"Опубліковано: {title}")
                    time.sleep(3) # Затримка, щоб Telegram не заблокував бота за спам

                # За один запуск публікуємо не більше 4 новин, щоб не перевантажувати стрічку каналу
                if new_posts >= 4:
                    break
        if new_posts >= 4:
            break

    save_history(history)

if __name__ == "__main__":
    if TELEGRAM_TOKEN and CHANNEL_ID:
        main()
    else:
        print("Помилка: Не знайдені TELEGRAM_TOKEN або CHANNEL_ID")
