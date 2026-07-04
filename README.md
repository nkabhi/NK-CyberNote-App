# Cybersecurity News Monitor — Your Free AI Agent

Monitors global data breach / hacking news 24x7 and sends you a digest on Telegram.
Runs entirely on free infrastructure. No credit card needed.

## How it works
1. GitHub Actions wakes the agent up once a day (6:00 AM UTC by default).
2. It pulls the latest articles from ~27 cybersecurity sources — breaking
   news outlets, vendor threat intel blogs, red team research, malware
   research, cloud security, and breach-tracking sites.
3. It filters for breach/hack/ransomware/vulnerability-related keywords.
4. It removes duplicate stories — the same breach often gets covered by
   5+ outlets with slightly different headlines; the agent detects near-
   identical titles and keeps only one.
5. It ranks everything by recency and keeps the **top 20**.
6. (Optional) It summarizes the batch using Google Gemini's free tier into
   one punchy line per story.
7. It sends you a single Telegram message with the digest + links.
8. It remembers what it already sent you across runs, so nothing repeats
   even if a story is still trending the next day.

## Setup (about 15 minutes, all free)

### 1. Create a Telegram bot (for delivery)
1. Open Telegram, message **@BotFather**, send `/newbot`, follow prompts.
2. It gives you a **bot token** — save it.
3. Message your new bot anything (e.g. "hi") so it can message you back.
4. Get your **chat ID**: visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   in a browser after step 3, and find `"chat":{"id": ...}` in the response.

### 2. Get a free Gemini API key (for AI summarization — optional but recommended)
1. Go to https://aistudio.google.com/apikey
2. Create a free API key (no card required).
3. Note: free tier covers Gemini 2.5 Flash / Flash-Lite generously; if you skip
   this step entirely, the agent still works — it'll just send raw headlines
   instead of an AI-written summary.

### 3. Put this code on GitHub
1. Create a new GitHub repo (private is fine, and free).
2. Upload all files in this folder to the repo (including the `.github` folder).

### 4. Add your secrets to the repo
In your repo: **Settings → Secrets and variables → Actions → New repository secret**
Add these three:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY` (optional)

### 5. Turn it on
- Go to the **Actions** tab in your repo → enable workflows if prompted.
- It will now run automatically every 6 hours, forever, for free
  (GitHub gives ~2,000 free Action minutes/month on private repos, unlimited
  on public repos — this job uses well under a minute per run).
- You can also click **Run workflow** manually anytime to test it immediately.

## Customizing your "employee"
- **Frequency**: edit the `cron` line in `.github/workflows/monitor.yml`.
  It's daily by default; change to `"0 */6 * * *"` for every 6 hours, etc.
  If you go more frequent than daily, also lower `LOOKBACK_HOURS` in the
  script to match (roughly: lookback hours ≈ hours between runs + 2).
- **How many stories per digest**: change `TOP_N` in `cyber_news_agent.py`
  (currently 20).
- **Sources**: add/remove RSS feeds in the `FEEDS` dict. A few sources on
  a wishlist (SOCRadar, Flashpoint, KELA, Intel471, CloudSEK, Ransomware.live,
  DataBreachToday) don't publish reliable public RSS feeds — most require
  a paid account. If you have access to one and it has an RSS/API endpoint,
  you can add it the same way.
- **Keywords**: tune the `KEYWORDS` list in the script to widen or narrow
  what counts as relevant. Set it to `[]` to disable filtering and pull
  everything from these sources, unfiltered.
- **Dedup sensitivity**: `is_duplicate()`'s `threshold` (0.72 by default)
  controls how similar two headlines need to be to count as the same story.
  Lower it (e.g. 0.6) to merge more aggressively; raise it (e.g. 0.85) if
  it's merging stories that are actually different.
- **More agents**: duplicate this pattern (fetch → filter → dedupe → rank →
  summarize → deliver) for any other "employee" you want — market news, job
  postings, competitor tracking, etc. Same free stack works for all of them.

## Cost
₹0 / $0 — GitHub Actions free tier + RSS feeds (free) + Gemini free tier +
Telegram Bot API (free). The only limits are rate limits, not bills.
