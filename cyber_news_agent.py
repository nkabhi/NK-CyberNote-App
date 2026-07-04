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
import re
import json
import time
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher

# ---------- CONFIG ----------
# Feeds are grouped just for readability - the script treats them all the same.
FEEDS = {
    # Breaking cybersecurity news
    "The Hacker News": "https://feeds.feedburner.com/TheHackersNews",
    "BleepingComputer": "https://www.bleepingcomputer.com/feed/",
    "Krebs on Security": "https://krebsonsecurity.com/feed/",
    "Dark Reading": "https://www.darkreading.com/rss.xml",
    "SecurityWeek": "https://www.securityweek.com/feed",
    "Help Net Security": "https://www.helpnetsecurity.com/feed/",
    "Infosecurity Magazine": "https://www.infosecurity-magazine.com/rss/news/",
    "CSO Online": "https://www.csoonline.com/feed/",

    # Vulnerability / official advisories
    "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
    "SANS Internet Storm Center": "https://isc.sans.edu/rssfeed.xml",
    "Exploit-DB": "https://www.exploit-db.com/rss.xml",

    # Blue team / vendor threat intel
    "Microsoft Security Blog": "https://www.microsoft.com/en-us/security/blog/feed/",
    "Cisco Talos": "https://blog.talosintelligence.com/rss/",
    "Palo Alto Unit 42": "https://unit42.paloaltonetworks.com/feed/",
    "CrowdStrike Blog": "https://www.crowdstrike.com/blog/feed/",
    "SentinelOne Labs": "https://www.sentinelone.com/labs/feed/",
    "Rapid7 Blog": "https://www.rapid7.com/blog/rss/",
    "Mandiant / Google Cloud Threat Intel": "https://cloud.google.com/blog/topics/threat-intelligence/rss/",

    # Red team / offensive security
    "Project Zero": "https://googleprojectzero.blogspot.com/feeds/posts/default",
    "PortSwigger Research": "https://portswigger.net/research/rss",
    "HackerOne Blog": "https://www.hackerone.com/blog.rss",

    # Malware research
    "Malwarebytes Labs": "https://www.malwarebytes.com/blog/feed/index.xml",
    "Securelist (Kaspersky)": "https://securelist.com/feed/",
    "ESET Research": "https://www.welivesecurity.com/en/rss/feed/",

    # Cloud security
    "AWS Security Blog": "https://aws.amazon.com/blogs/security/feed/",

    # Ransomware / breach tracking
    "Have I Been Pwned": "https://haveibeenpwned.com/rss",
    "The Record (Recorded Future News)": "https://therecord.media/feed",
}

# Broad net on purpose - the goal is coverage, not a narrow filter.
# Set to [] to disable keyword filtering entirely and just take everything.
KEYWORDS = [
    "breach", "hack", "hacked", "hacking", "ransomware", "leak", "leaked",
    "cyberattack", "cyber attack", "exploit", "vulnerability", "cve",
    "data exposed", "stolen data", "phishing", "malware", "zero-day",
    "compromised", "data theft", "credential", "attack", "threat actor",
    "campaign", "backdoor", "spyware", "trojan"
]

LOOKBACK_HOURS = 26          # slightly over 24h so a daily run never has gaps
TOP_N = 20                   # how many stories to send per digest
SEEN_FILE = "seen_articles.json"   # prevents duplicate alerts across runs

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")       # optional but recommended
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


# ---------- STEP 1: FETCH ----------
def normalize_title(title):
    """Lowercase, strip punctuation/extra spaces - used for dedup comparison."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_duplicate(title, seen_titles, threshold=0.72):
    """Catches the same story reported by multiple outlets with different wording."""
    norm = normalize_title(title)
    for existing in seen_titles:
        if SequenceMatcher(None, norm, existing).ratio() >= threshold:
            return True
    return False


def fetch_recent_articles():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen_ids = load_seen()
    candidates = []

    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"Failed to fetch {source}: {e}")
            continue

        if getattr(feed, "bozo", False) and not feed.entries:
            print(f"Warning: {source} feed may be broken ({url})")
            continue

        for entry in feed.entries:
            uid = entry.get("id", entry.get("link"))
            if uid in seen_ids:
                continue

            # Prefer published date; fall back to updated; if neither, keep it
            # rather than silently dropping it (better to over-include).
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            else:
                pub_dt = datetime.now(timezone.utc)

            title = entry.get("title", "").strip()
            summary = re.sub("<[^<]+?>", "", entry.get("summary", ""))  # strip HTML tags
            text_blob = (title + " " + summary).lower()

            relevant = (not KEYWORDS) or any(k in text_blob for k in KEYWORDS)
            if not relevant:
                continue

            candidates.append({
                "id": uid,
                "source": source,
                "title": title,
                "link": entry.get("link"),
                "summary": summary[:500],
                "published": pub_dt,
            })

    # Sort newest first
    candidates.sort(key=lambda x: x["published"], reverse=True)

    # Dedup near-identical headlines across different outlets
    deduped = []
    seen_titles_norm = []
    for item in candidates:
        norm = normalize_title(item["title"])
        if is_duplicate(item["title"], seen_titles_norm):
            continue
        seen_titles_norm.append(norm)
        deduped.append(item)

    top_items = deduped[:TOP_N]
    return top_items, seen_ids


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
        f"{idx}. [{i['source']}] {i['title']}: {i['summary']}"
        for idx, i in enumerate(items, start=1)
    )
    prompt = (
        "You are a cybersecurity analyst preparing a daily digest for a busy "
        f"reader. Below are up to {TOP_N} distinct news items about breaches, "
        "hacks, ransomware, and vulnerabilities from the last 24 hours. "
        "Write a numbered list (matching the numbers given) where each item "
        "is ONE punchy sentence (max ~25 words) capturing what happened and "
        "why it matters. Plain text only, no markdown formatting, no extra "
        "commentary before or after the list:\n\n"
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
    lines = [f"🛡️ Top {len(items)} Cybersecurity Stories — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. [{item['source']}] {item['title']}\n{item['link']}\n")
    return "\n".join(lines)


# ---------- MAIN ----------
def main():
    items, seen = fetch_recent_articles()

    if not items:
        print("No new breach/hack news in this window.")
        return

    print(f"Found {len(items)} new relevant, deduplicated articles.")

    summary = summarize_with_gemini(items)

    if isinstance(summary, str):
        header = (
            f"🛡️ Top {len(items)} Cybersecurity Stories — "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        )
        # Pair each numbered summary line with its source link right below it
        link_lines = "\n".join(
            f"{idx}. 🔗 {i['link']}" for idx, i in enumerate(items, start=1)
        )
        message = header + summary + "\n\n" + link_lines
    else:
        message = format_raw_digest(items)

    send_telegram(message)

    # mark these as seen so we don't repeat them next run
    seen.update(i["id"] for i in items)
    save_seen(seen)


if __name__ == "__main__":
    main()
