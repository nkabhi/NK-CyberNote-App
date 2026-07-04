"""
Cybersecurity News Monitor - AI Agent
--------------------------------------
Fetches latest data breach / hacking news from multiple free RSS feeds,
uses a free LLM (Google Gemini) to summarize + prioritize, and sends
a digest to you via Telegram.

Runs on a schedule (see .github/workflows/monitor.yml) so it behaves
like a 24x7 "employee" without costing anything or needing your PC on.
"""

import os
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta

# ---------- CONFIG ----------
FEEDS = {
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "Dark Reading": "https://www.darkreading.com/rss.xml",
    "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
}

KEYWORDS = [
    "breach", "hack", "ransomware", "leak", "leaked", "cyberattack",
    "exploit", "vulnerability", "data exposed", "stolen data", "phishing",
    "malware", "zero-day", "compromised"
]

LOOKBACK_HOURS = 6          # only consider articles newer than this
SEEN_FILE = "seen_articles.json"   # prevents duplicate alerts across runs

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")       # optional but recommended
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- STEP 1: FETCH ----------
def fetch_recent_articles():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen = load_seen()
    new_items = []

    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"Failed to fetch {source}: {e}")
            continue

        for entry in feed.entries:
            uid = entry.get("id", entry.get("link"))
            if uid in seen:
                continue

            # parse publish time if available; if missing, include it anyway
            published = entry.get("published_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")
            text_blob = (title + " " + summary).lower()

            if any(k in text_blob for k in KEYWORDS):
                new_items.append({
                    "id": uid,
                    "source": source,
                    "title": title,
                    "link": entry.get("link"),
                    "summary": summary[:500],
                })

    return new_items, seen


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_set):
    # keep the file from growing forever - cap at last 500 ids
    trimmed = list(seen_set)[-500:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


# ---------- STEP 2: SUMMARIZE (optional, free Gemini tier) ----------
def summarize_with_gemini(items):
    if not GEMINI_API_KEY or not items:
        return items  # skip AI step if no key set, just pass through raw items

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    bullet_list = "\n".join(
        f"- [{i['source']}] {i['title']}: {i['summary']}" for i in items
    )
    prompt = (
        "You are a cybersecurity analyst. Below are raw news items about "
        "data breaches / hacks. Write a short, punchy digest (max 150 words), "
        "grouped by severity if possible, plain text, no markdown headers:\n\n"
        f"{bullet_list}"
    )

    try:
        resp = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return text
    except Exception as e:
        print(f"Gemini summarization failed, falling back to raw list: {e}")
        return items


# ---------- STEP 3: DELIVER ----------
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured. Printing digest instead:\n")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram has a 4096 char limit per message; split if needed
    for i in range(0, len(message), 4000):
        chunk = message[i:i + 4000]
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": False,
        })
        time.sleep(1)


def format_raw_digest(items):
    lines = [f"🛡️ Cybersecurity Digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]
    for item in items:
        lines.append(f"• [{item['source']}] {item['title']}\n{item['link']}\n")
    return "\n".join(lines)


# ---------- MAIN ----------
def main():
    items, seen = fetch_recent_articles()

    if not items:
        print("No new breach/hack news in this window.")
        return

    print(f"Found {len(items)} new relevant articles.")

    summary = summarize_with_gemini(items)

    if isinstance(summary, str):
        header = f"🛡️ Cybersecurity Digest — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        links = "\n".join(f"🔗 {i['link']}" for i in items)
        message = header + summary + "\n\n" + links
    else:
        message = format_raw_digest(items)

    send_telegram(message)

    # mark these as seen so we don't repeat them next run
    seen.update(i["id"] for i in items)
    save_seen(seen)


if __name__ == "__main__":
    main()
