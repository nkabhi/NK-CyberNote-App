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
    "Mandiant / Google Cloud Threat Intel": "https://cloudblog.withgoogle.com/topics/threat-intelligence/rss/",

    # Red team / offensive security
    "Project Zero": "https://googleprojectzero.blogspot.com/feeds/posts/default",
    "PortSwigger Research": "https://portswigger.net/research/rss",

    # Malware research
    "Malwarebytes Labs": "https://www.malwarebytes.com/blog/feed/index.xml",
    "Securelist (Kaspersky)": "https://securelist.com/feed/",
    "ESET Research": "https://www.welivesecurity.com/en/feed/",

    # Cloud security
    "AWS Security Blog": "https://aws.amazon.com/blogs/security/feed/",

    # Ransomware / breach tracking
    "Have I Been Pwned (new breaches)": "https://feeds.feedburner.com/HaveIBeenPwnedLatestBreaches",
    "Troy Hunt's Blog": "https://www.troyhunt.com/rss/",
    "The Record (Recorded Future News)": "https://therecord.media/feed",
}

# These sources are ALL dedicated cybersecurity outlets - every article on them
# is already "cybersecurity news" by definition. A keyword filter on top of
# that is redundant and was the main reason you were only getting 1-9 stories:
# most real articles (CVE writeups, research posts, advisories) don't literally
# contain the word "hack" or "breach" even though they're 100% relevant.
# Left empty by default = take everything, ranked by recency. If you want a
# narrower feed later (e.g. only breach/ransomware, skip general research),
# add words back here.
KEYWORDS = []

MAX_PER_SOURCE = 3           # cap so one vendor's blog can't dominate the digest
LOOKBACK_HOURS = 26          # slightly over 24h so a daily run never has gaps
TOP_N = 20                   # how many stories to send per digest
SEEN_FILE = "seen_articles.json"   # prevents duplicate alerts across runs

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")       # optional but recommended
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE")        # e.g. +91XXXXXXXXXX, optional
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY")      # from CallMeBot, optional


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
        feed = None
        last_error = None
        for attempt in range(2):  # try twice before giving up on this source
            try:
                # Some sites (e.g. CISA) block requests with no/unusual User-Agent
                # and return 403. Fetching via requests with a normal browser UA
                # first, then handing the raw bytes to feedparser, avoids this.
                resp = requests.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
                    timeout=20,
                )
                resp.raise_for_status()
                feed = feedparser.parse(resp.content)
                break
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(3)  # brief pause, then one retry

        if feed is None:
            print(f"[FEED FAIL] {source}: exception after retry - {last_error}")
            continue

        total_entries = len(feed.entries)
        if total_entries == 0:
            status = getattr(feed, "status", "unknown")
            print(f"[FEED EMPTY] {source}: 0 entries returned (http status: {status}, url: {url})")
            continue

        kept_this_source = 0
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
                "summary": summary[:1500],
                "published": pub_dt,
            })
            kept_this_source += 1

        print(f"[FEED OK] {source}: {total_entries} entries fetched, {kept_this_source} kept after filtering")

    print(f"\n[SUMMARY] {len(candidates)} candidate articles across all sources before dedup\n")

    # Sort newest first
    candidates.sort(key=lambda x: x["published"], reverse=True)

    # Dedup near-identical headlines across different outlets, and cap how
    # many stories any single source can contribute (prevents one vendor's
    # blog - which may post several product/marketing pieces a day - from
    # crowding out real incident coverage from other outlets).
    deduped = []
    seen_titles_norm = []
    per_source_count = {}
    for item in candidates:
        norm = normalize_title(item["title"])
        if is_duplicate(item["title"], seen_titles_norm):
            continue
        if per_source_count.get(item["source"], 0) >= MAX_PER_SOURCE:
            continue
        seen_titles_norm.append(norm)
        per_source_count[item["source"]] = per_source_count.get(item["source"], 0) + 1
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

    bullet_list = "\n\n".join(
        f"STORY {idx}\nSource: {i['source']}\nTitle: {i['title']}\nDetails: {i['summary']}"
        for idx, i in enumerate(items, start=1)
    )
    prompt = (
        "You are a threat intelligence analyst preparing a detailed daily "
        f"briefing. Below are {len(items)} raw cybersecurity news items from "
        "the last 24 hours.\n\n"
        "STRUCTURE YOUR RESPONSE IN TWO PARTS:\n\n"
        "PART 1 - EXECUTIVE SUMMARY (write this first, 4-6 sentences, plain "
        "paragraph, no bullets): summarize the overall picture across all "
        "stories today - key themes, the most severe/urgent items, any "
        "named threat actors or CVEs that stand out, and the general threat "
        "landscape for the day. This is a busy reader's 30-second overview "
        "before they scan the details below. Start this section with the "
        "plain text label 'SUMMARY:' on its own line before the paragraph.\n\n"
        "PART 2 - STORY DETAILS: start this section with the plain text "
        "label 'DETAILS:' on its own line, then for EACH story, write one "
        "entry like this:\n\n"
        "N. [Headline in your own words, one line]\n"
        "   - Threat actor: <only include this line if the source names one>\n"
        "   - TTPs / attack vector: <only include if the source specifies a "
        "mechanism - e.g. phishing, exploited CVE-XXXX-XXXXX, supply chain "
        "compromise, credential stuffing, ransomware double-extortion>\n"
        "   - CVE / vulnerability: <only include if the source names an "
        "actual CVE ID or specific named vulnerability>\n"
        "   - Impact: <only include if the source specifies who/what was "
        "affected>\n"
        "   - Why it matters: <always include - one clause on significance>\n"
        "   - How to protect / respond: <always include - 2-3 concrete "
        "defensive steps, based on the source if it recommends any, "
        "otherwise standard best-practice mitigation for that attack type>\n\n"
        "CRITICAL RULE ON MISSING FIELDS: if a field's information isn't "
        "present in the source text (e.g. no threat actor is named, no CVE "
        "is mentioned), SKIP that entire line completely - do not write it "
        "with 'not attributed', 'not specified', 'no CVE mentioned', or any "
        "placeholder text. Just omit the line. 'Why it matters' and 'How to "
        "protect / respond' should always be included since those can "
        "always be reasoned about even without those specific facts.\n\n"
        "OTHER STRICT RULES:\n"
        "- Base every fact ONLY on the details given below for that story. "
        "NEVER invent, guess, or fabricate a threat actor name, CVE number, "
        "or technique that isn't in the source text.\n"
        "- Do not include any URLs or links.\n"
        "- Plain text only, no markdown bold/asterisks/headers.\n"
        "- Number stories 1 through "
        f"{len(items)}, matching STORY numbers below, in the same order.\n\n"
        f"{bullet_list}"
    )

    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 8192},
                },
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return text
        except Exception as e:
            wait = (attempt + 1) * 5  # 5s, 10s, 15s
            print(f"Gemini call failed (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                print(f"Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print("Gemini summarization failed after 3 attempts, falling back to raw list.")
                return items


# ---------- STEP 3: DELIVER ----------
def send_whatsapp(message):
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        print("WhatsApp not configured (WHATSAPP_PHONE/WHATSAPP_APIKEY missing) - skipping.")
        return

    url = "https://api.callmebot.com/whatsapp.php"
    # CallMeBot's free WhatsApp API is intended for shorter personal alerts,
    # so we chunk more conservatively than Telegram (which allows ~4096 chars).
    CHUNK_SIZE = 1500
    for i in range(0, len(message), CHUNK_SIZE):
        chunk = message[i:i + CHUNK_SIZE]
        try:
            resp = requests.get(
                url,
                params={"phone": WHATSAPP_PHONE, "text": chunk, "apikey": WHATSAPP_APIKEY},
                timeout=20,
            )
            if resp.status_code != 200:
                print(f"WhatsApp send failed (status {resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            print(f"WhatsApp send error: {e}")
        time.sleep(2)  # CallMeBot rate-limits rapid consecutive messages


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
    lines = [
        f"🛡️ Top {len(items)} Cybersecurity Stories — "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n",
        "(Note: set GEMINI_API_KEY to get threat actor + TTP breakdowns per "
        "story. Showing headlines only for now.)\n",
    ]
    for idx, item in enumerate(items, start=1):
        lines.append(f"{idx}. [{item['source']}] {item['title']}")
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
        message = header + summary
    else:
        message = format_raw_digest(items)

    send_telegram(message)
    send_whatsapp(message)

    # mark these as seen so we don't repeat them next run
    seen.update(i["id"] for i in items)
    save_seen(seen)


if __name__ == "__main__":
    main()
