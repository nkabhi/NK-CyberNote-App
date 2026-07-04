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
        except Exception as e:
            print(f"[FEED FAIL] {source}: exception - {e}")
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

    bullet_list = "\n\n".join(
        f"STORY {idx}\nSource: {i['source']}\nTitle: {i['title']}\nDetails: {i['summary']}"
        for idx, i in enumerate(items, start=1)
    )
    prompt = (
        "You are a top threat intelligence analyst preparing a detailed daily "
        f"briefing. Below are {len(items)} raw cybersecurity news items from "
        "the last 24 hours. For EACH story, write one entry in this exact "
        "format:\n\n"
        "N. [Headline in your own words, one line]\n"
        "   - Threat actor: <name if the source text names one, otherwise "
        "'Not attributed in source'>\n"
        "   - TTPs / attack vector: <specific technique(s) mentioned - e.g. "
        "phishing, exploited CVE-XXXX-XXXXX, supply chain compromise, "
        "credential stuffing, malicious npm package, ransomware double-"
        "extortion, etc. If the source doesn't specify a mechanism, write "
        "'Not detailed in source'>\n"
        "   - CVE / vulnerability: <exact CVE ID(s) and a one-clause "
        "description of the flaw if the source mentions one (e.g. 'CVE-2026-"
        "XXXXX - unauthenticated RCE in X'). If no CVE or specific "
        "vulnerability is named, write 'No CVE mentioned in source'>\n"
        "   - Impact: <who/what was affected, in one short clause>\n"
        "   - Why it matters: <one clause>\n"
        "   - How to protect / respond: <2-3 concrete, actionable defensive "
        "steps - e.g. 'patch to version X', 'apply CVE-XXXX-XXXXX fix', "
        "'rotate exposed credentials', 'block indicator Y at the firewall/"
        "email gateway', 'enable MFA', 'monitor for IOC Z'. Base this on "
        "what the source recommends if stated; otherwise give standard best-"
        "practice mitigation for that specific attack type (patching, "
        "network segmentation, credential rotation, EDR detection rules, "
        "user awareness, etc.) - but don't fabricate specific version "
        "numbers or IOCs that aren't in the source.>\n\n"
        "STRICT RULES:\n"
        "- Base every fact ONLY on the details given below for that story. "
        "NEVER invent, guess, or fabricate a threat actor name, CVE number, "
        "or technique that isn't in the source text - if it's not there, say "
        "so explicitly rather than making one up. A wrong CVE number is "
        "worse than no CVE number.\n"
        "- Do not include any URLs or links.\n"
        "- Plain text only, no markdown bold/asterisks/headers.\n"
        "- Keep each field to one short clause or a tight list - this is a "
        "scan-in-a-minute briefing, not an essay.\n"
        "- Number stories 1 through "
        f"{len(items)}, matching STORY numbers below, in the same order.\n\n"
        f"{bullet_list}"
    )

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

    # mark these as seen so we don't repeat them next run
    seen.update(i["id"] for i in items)
    save_seen(seen)


if __name__ == "__main__":
    main()
