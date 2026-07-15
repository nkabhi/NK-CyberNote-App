"""
Cybersecurity News Monitor - AI Agent
--------------------------------------
Fetches the latest data breach / hacking news from multiple free RSS feeds,
groups results by source website (max 5 latest, never-seen-before stories
per site), uses a free LLM (Google Gemini) to write a headline + summary +
threat analysis for each, and sends the digest to you via Telegram/WhatsApp.

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

# Homepage link shown next to each source's section header in the digest
# (not per-article links - you asked for no per-story links, just the site
# name/homepage once per section).
SOURCE_HOMEPAGE = {
    "The Hacker News": "https://thehackernews.com",
    "BleepingComputer": "https://www.bleepingcomputer.com",
    "Krebs on Security": "https://krebsonsecurity.com",
    "Dark Reading": "https://www.darkreading.com",
    "SecurityWeek": "https://www.securityweek.com",
    "Help Net Security": "https://www.helpnetsecurity.com",
    "Infosecurity Magazine": "https://www.infosecurity-magazine.com",
    "CSO Online": "https://www.csoonline.com",
    "CISA Advisories": "https://www.cisa.gov/cybersecurity-advisories",
    "SANS Internet Storm Center": "https://isc.sans.edu",
    "Exploit-DB": "https://www.exploit-db.com",
    "Microsoft Security Blog": "https://www.microsoft.com/en-us/security/blog",
    "Cisco Talos": "https://blog.talosintelligence.com",
    "Palo Alto Unit 42": "https://unit42.paloaltonetworks.com",
    "CrowdStrike Blog": "https://www.crowdstrike.com/blog",
    "SentinelOne Labs": "https://www.sentinelone.com/labs",
    "Rapid7 Blog": "https://www.rapid7.com/blog",
    "Mandiant / Google Cloud Threat Intel": "https://cloud.google.com/blog/topics/threat-intelligence",
    "Project Zero": "https://googleprojectzero.blogspot.com",
    "PortSwigger Research": "https://portswigger.net/research",
    "Malwarebytes Labs": "https://www.malwarebytes.com/blog",
    "Securelist (Kaspersky)": "https://securelist.com",
    "ESET Research": "https://www.welivesecurity.com",
    "AWS Security Blog": "https://aws.amazon.com/blogs/security",
    "Have I Been Pwned (new breaches)": "https://haveibeenpwned.com",
    "Troy Hunt's Blog": "https://www.troyhunt.com",
    "The Record (Recorded Future News)": "https://therecord.media",
}

# These sources are ALL dedicated cybersecurity outlets - every article on them
# is already "cybersecurity news" by definition, so no keyword filter is
# applied (left empty = take everything, ranked by recency).
KEYWORDS = []

MAX_PER_SOURCE = 5            # latest unseen stories to include, per website
MIN_PER_SOURCE = 2            # best-effort target - see note in fetch_recent_articles()
LOOKBACK_HOURS = 30           # wider-than-24h window so borderline-recent stories aren't missed
SEEN_FILE = "seen_articles.json"    # prevents the same story ever being sent twice, across days

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
    """Catches the same story reported twice (rare within a single source)."""
    norm = normalize_title(title)
    for existing in seen_titles:
        if SequenceMatcher(None, norm, existing).ratio() >= threshold:
            return True
    return False


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_set):
    # keep the file from growing forever - cap at last 2000 ids
    # (raised from 500 since we now track up to ~26 sources x 5/day)
    trimmed = list(seen_set)[-2000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


def fetch_recent_articles():
    """
    Returns an ORDERED dict: {source_name: [item, item, ...]}
    - Only sources with at least 1 new (never-before-sent) story are included.
    - Each source's list is newest-first, capped at MAX_PER_SOURCE.
    - Stories already sent on a previous day (tracked in seen_articles.json)
      are permanently excluded - "if you already sent that news, don't send
      it again" applies to every source below, forever.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    seen_ids = load_seen()
    grouped = {}   # source -> list of items, preserves FEEDS order

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

        source_candidates = []
        for entry in feed.entries:
            uid = entry.get("id", entry.get("link"))
            if uid in seen_ids:
                continue  # already sent this one on a previous day - skip forever

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

            source_candidates.append({
                "id": uid,
                "source": source,
                "title": title,
                "link": entry.get("link"),
                "summary": summary[:1500],
                "published": pub_dt,
            })

        # newest first, dedup near-identical titles within this source, cap at MAX_PER_SOURCE
        source_candidates.sort(key=lambda x: x["published"], reverse=True)
        deduped = []
        seen_titles_norm = []
        for item in source_candidates:
            if is_duplicate(item["title"], seen_titles_norm):
                continue
            seen_titles_norm.append(normalize_title(item["title"]))
            deduped.append(item)
            if len(deduped) >= MAX_PER_SOURCE:
                break

        print(f"[FEED OK] {source}: {total_entries} entries fetched, {len(deduped)} new stories selected")
        if 0 < len(deduped) < MIN_PER_SOURCE:
            print(f"  Note: {source} only has {len(deduped)} new story right now (target min is {MIN_PER_SOURCE}) - sending anyway rather than risk losing it.")

        if deduped:
            grouped[source] = deduped

    total = sum(len(v) for v in grouped.values())
    print(f"\n[SUMMARY] {total} total new stories across {len(grouped)} sources with activity\n")
    print("[FINAL DIGEST BREAKDOWN]")
    for src, items in grouped.items():
        capped_note = " (hit MAX_PER_SOURCE cap - more may exist)" if len(items) >= MAX_PER_SOURCE else ""
        print(f"  {src}: {len(items)} stories{capped_note}")

    return grouped, seen_ids


# ---------- STEP 2: SUMMARIZE (optional, free Gemini tier) ----------
def summarize_source(source, items):
    """
    Takes ONE source's list of items and returns plain-text numbered
    headline + summary for just that source (no threat actor/CVE/TTP/
    impact/mitigation fields - just what happened, in plain language).
    Returns None if Gemini isn't configured or the call fails (caller
    falls back to raw headlines for this source instead).
    """
    if not GEMINI_API_KEY:
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    story_lines = "\n\n".join(
        f"STORY {idx}\nTitle: {item['title']}\n"
        f"Published: {item['published'].strftime('%d %b %Y, %H:%M UTC')}\n"
        f"Details: {item['summary']}"
        for idx, item in enumerate(items, start=1)
    )

    prompt = (
        "You are summarizing cybersecurity news from a single source for a "
        "quick daily briefing. Below are "
        f"{len(items)} raw news items. For EACH one, write:\n\n"
        "N. [Headline in your own words, one line, no markdown]\n"
        "   Date: <copy the exact 'Published' value given below for this "
        "story - do not compute, guess, or reformat it, just copy it "
        "verbatim>\n"
        "   Summary: <2-3 plain-language sentences covering what happened, "
        "who/what was involved, and why it's notable>\n\n"
        "STRICT RULES:\n"
        "- Base every fact ONLY on the details given below for that story. "
        "Never invent or guess facts not present in the source text.\n"
        "- Do not include any URLs or links.\n"
        "- Plain text only, no markdown bold/asterisks/headers.\n"
        "- Number stories 1 through "
        f"{len(items)}, matching STORY numbers below, in the same order.\n\n"
        f"{story_lines}"
    )

    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 2048},
                },
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return text
        except Exception as e:
            wait = (attempt + 1) * 5
            print(f"  Gemini call failed for {source} (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(wait)
            else:
                print(f"  Falling back to raw headlines for {source}.")
                return None


def format_raw_source(items):
    """Fallback (no Gemini) - just numbered headlines + dates for one source."""
    lines = []
    for idx, item in enumerate(items, start=1):
        date_str = item["published"].strftime("%d %b %Y, %H:%M UTC")
        lines.append(f"{idx}. [{date_str}] {item['title']}")
    return "\n".join(lines)


def escape_markdown(text):
    """Escape Telegram legacy-Markdown special characters in dynamic text so
    a stray * _ ` [ in a headline/summary can't break message formatting."""
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, "\\" + ch)
    return text


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


def build_html_digest(source_blocks):
    """
    source_blocks: list of dicts, each {source, homepage, count, body}
    Builds one self-contained, nicely styled HTML page covering the whole
    day's digest, grouped by website.
    """
    now_str = datetime.now(timezone.utc).strftime('%d %B %Y, %H:%M UTC')
    total_stories = sum(b["count"] for b in source_blocks)

    sections_html = ""
    for block in source_blocks:
        # Convert "N. Headline\n   Date: ...\n   Summary: ..." into styled
        # story cards rather than a flat <br>-separated blob.
        story_html = ""
        raw_entries = re.split(r"\n(?=\d+\.\s)", block["body"].strip())
        for entry in raw_entries:
            if not entry.strip():
                continue
            lines = entry.strip().split("\n")
            headline = re.sub(r"^\d+\.\s*", "", lines[0]).strip()
            date_line = next((l.strip() for l in lines[1:] if l.strip().lower().startswith("date:")), "")
            date_text = date_line.split(":", 1)[1].strip() if ":" in date_line else ""
            summary_line = next((l.strip() for l in lines[1:] if l.strip().lower().startswith("summary:")), "")
            summary_text = summary_line.split(":", 1)[1].strip() if ":" in summary_line else " ".join(lines[1:]).strip()

            story_html += f"""
            <div class="story">
              <div class="story-headline">{headline}</div>
              {f'<div class="story-date">{date_text}</div>' if date_text else ''}
              <div class="story-summary">{summary_text}</div>
            </div>
            """

        sections_html += f"""
        <section class="source-section">
          <div class="source-header">
            <h2><a href="{block['homepage']}" target="_blank" rel="noopener">{block['source']}</a></h2>
            <span class="badge">{block['count']} {'story' if block['count'] == 1 else 'stories'}</span>
          </div>
          {story_html}
        </section>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cybersecurity Digest — {now_str}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    max-width: 860px; margin: 0 auto; padding: 32px 20px 60px;
    line-height: 1.65; color: #1c1f26; background: #f4f5f7;
  }}
  .masthead {{
    background: linear-gradient(135deg, #1a1a2e 0%, #7a1f2b 100%);
    color: #fff; border-radius: 14px; padding: 28px 30px; margin-bottom: 28px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.15);
  }}
  .masthead h1 {{ margin: 0 0 6px 0; font-size: 26px; letter-spacing: -0.3px; }}
  .masthead .meta {{ font-size: 13.5px; opacity: 0.85; }}
  .masthead .stat {{
    display: inline-block; margin-top: 14px; background: rgba(255,255,255,0.12);
    padding: 6px 14px; border-radius: 20px; font-size: 13px; font-weight: 600;
  }}
  .source-section {{
    background: #fff; border: 1px solid #e4e6ea; border-radius: 12px;
    padding: 22px 26px; margin-bottom: 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .source-header {{
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 2px solid #f0f1f3; padding-bottom: 12px; margin-bottom: 16px;
  }}
  .source-header h2 {{ margin: 0; font-size: 18px; }}
  .source-header a {{ color: #1a4b8c; text-decoration: none; }}
  .source-header a:hover {{ text-decoration: underline; }}
  .badge {{
    background: #eef2ff; color: #3b4ea0; font-size: 12px; font-weight: 700;
    padding: 4px 11px; border-radius: 14px; white-space: nowrap;
  }}
  .story {{ padding: 12px 0; border-bottom: 1px solid #f2f3f5; }}
  .story:last-child {{ border-bottom: none; padding-bottom: 0; }}
  .story-headline {{ font-size: 15.5px; font-weight: 650; color: #14161a; margin-bottom: 4px; }}
  .story-date {{ font-size: 12px; color: #8a8f98; margin-bottom: 6px; font-weight: 500; }}
  .story-summary {{ font-size: 14px; color: #444a54; }}
  .footer {{ text-align: center; color: #9aa0a8; font-size: 12px; margin-top: 34px; }}
</style>
</head>
<body>
  <div class="masthead">
    <h1>🛡️ Cybersecurity Digest</h1>
    <div class="meta">Generated {now_str}</div>
    <div class="stat">{total_stories} new stories across {len(source_blocks)} sources</div>
  </div>
  {sections_html}
  <div class="footer">Built automatically · Sources link to their original site</div>
</body>
</html>"""
    return html


def send_telegram_document(filepath, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - skipping HTML file attachment.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (os.path.basename(filepath), f, "text/html")},
                timeout=30,
            )
        if resp.status_code != 200:
            print(f"Telegram file send failed ({resp.status_code}): {resp.text[:200]}")
        else:
            print("HTML digest file sent to Telegram successfully.")
    except Exception as e:
        print(f"Telegram file send error: {e}")


def send_telegram(message, use_markdown=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured. Printing digest instead:\n")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram has a 4096 char limit per message; split if needed
    for i in range(0, len(message), 4000):
        chunk = message[i:i + 4000]
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": False,
        }
        if use_markdown:
            payload["parse_mode"] = "Markdown"
        resp = requests.post(url, data=payload)
        if resp.status_code != 200 and use_markdown:
            # Formatting broke the send (e.g. an unescaped char slipped through) -
            # retry once as plain text so the message still gets delivered.
            print(f"Telegram Markdown send failed ({resp.status_code}), retrying as plain text: {resp.text[:200]}")
            payload.pop("parse_mode", None)
            requests.post(url, data=payload)
        time.sleep(1)





# ---------- MAIN ----------
def main():
    grouped, seen = fetch_recent_articles()

    if not grouped:
        print("No new stories found on any source in this window.")
        return

    total = sum(len(v) for v in grouped.values())
    print(f"Found {total} new stories across {len(grouped)} sources. Building HTML digest (no per-site chat messages).")

    source_blocks_for_html = []

    for source, items in grouped.items():
        homepage = SOURCE_HOMEPAGE.get(source, "")

        body = summarize_source(source, items)
        if body is None:
            body = format_raw_source(items)
            note = (
                "\n(Note: set GEMINI_API_KEY for per-story summaries. "
                "Showing headlines only for this source.)"
            )
        else:
            note = ""

        print(f"  Prepared {source}: {len(items)} stories")
        source_blocks_for_html.append({
            "source": source,
            "homepage": homepage,
            "count": len(items),
            "body": body + note,
        })

        # Mark this source's stories as permanently seen right away, so a
        # later failure doesn't cause a resend of sources already processed.
        seen.update(item["id"] for item in items)
        save_seen(seen)

        time.sleep(2)  # small pause between Gemini calls to stay well under rate limits

    html_content = build_html_digest(source_blocks_for_html)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html_path = f"cyber_digest_{date_str}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    caption = f"🛡️ Cybersecurity Digest — {total} new stories across {len(grouped)} sources — {date_str}"
    send_telegram_document(html_path, caption=caption)

    # WhatsApp can't receive file attachments via the free CallMeBot API,
    # so it gets a short text heads-up instead of the full page.
    send_whatsapp(f"{caption}\nCheck Telegram for the full webpage.")

    print("\nDigest complete.")


if __name__ == "__main__":
    main()
