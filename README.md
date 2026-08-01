# Cyber Digest Bot

A zero-cost Telegram digest bot. Runs on GitHub Actions every 12 hours, pulls
RSS/Atom feeds across security, tooling, OS, AI, dev, design and hardware,
dedupes against previous runs, ranks by keyword relevance, and posts a
categorized digest to a Telegram channel.

No server. No VPS. No credit card. No always-on laptop.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Fetch, filter, dedupe, score, render, send |
| `feeds.yaml` | All sources, grouped into categories |
| `state/seen.json` | Rolling list of link hashes already sent (auto-committed) |
| `.github/workflows/digest.yml` | Cron schedule + runner definition |
| `requirements.txt` | `feedparser`, `requests`, `PyYAML` |

---

## Setup

### 1. Create the bot
Telegram → `@BotFather` → `/newbot` → pick a name and a username ending in `bot`.
Copy the token: `8123456789:AAH...`

### 2. Create a channel and get its ID
1. New Channel → Private → name it.
2. Add your bot as **Admin** with **Post Messages** permission.
3. Post any message in the channel, forward it to `@userinfobot`, or open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Channel IDs look like `-1001234567890`. A personal chat ID is a plain positive number.

### 3. Push this repo to GitHub
Public repo = unlimited free Actions minutes. Private = 2,000 min/month, also plenty
(each run costs about 1–2 minutes).

### 4. Add secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 5. Enable and test
Actions tab → enable workflows → `Cyber Digest` → **Run workflow**.

---

## Tuning

Set these in `.github/workflows/digest.yml` under `env:`

| Variable | Default | Effect |
|---|---|---|
| `LOOKBACK_HOURS` | `13` | Window of "fresh" items. 12h cycle + 1h overlap. |
| `MAX_PER_CATEGORY` | `8` | Cap per section. Lower it if the digest is too long. |
| `MAX_PER_FEED` | `3` | Stops one noisy feed from dominating. |
| `MIN_SCORE` | `0` | Raise to `2` or `3` for high-signal only. |
| `SEND_EMPTY` | `0` | `1` sends a "nothing new" message. |
| `DRY_RUN` | `0` | `1` prints to stdout instead of Telegram. |

Per-feed `boost:` in `feeds.yaml` adds to the score of every item from that source.

---

## Adding sources

**Any GitHub project's releases:**
`https://github.com/<owner>/<repo>/releases.atom`

**Any GitHub project's commits:**
`https://github.com/<owner>/<repo>/commits/<branch>.atom`

**Any subreddit:**
`https://www.reddit.com/r/<sub>/.rss`

**Reddit search:**
`https://www.reddit.com/search.rss?q=<query>&sort=new&restrict_sr=off`

**Hacker News with a score threshold:**
`https://hnrss.org/newest?q=<query>&points=100`

**Google Alerts → RSS** (best way to cover scholarships, Arabic keywords, niche topics):
1. Go to `google.com/alerts`
2. Create an alert, click **Show options**
3. Set **Deliver to: RSS feed**
4. Copy the feed URL from the RSS icon and paste it into `feeds.yaml`

**YouTube channel:**
`https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>`

---

## Local testing

```bash
pip install -r requirements.txt
DRY_RUN=1 python bot.py            # prints the digest, sends nothing
```

To validate a single feed URL:

```bash
python -c "import feedparser,sys; f=feedparser.parse(sys.argv[1]); print(len(f.entries), f.feed.get('title'))" "<URL>"
```

---

## Known limits

- GitHub Actions cron can fire 5–30 minutes late under load. Not a problem for a 12h digest.
- Scheduled workflows are auto-disabled after 60 days with no repository activity.
  Commits made by `GITHUB_TOKEN` do not always reset that timer. Push a manual
  commit every couple of months, or re-enable from the Actions tab when it happens.
- Telegram messages cap at 4096 characters. `bot.py` splits at 3800.
- Reddit rate-limits aggressive polling. The script sleeps 0.4s between feeds and
  sends a browser User-Agent.
- If `state/seen.json` is deleted, the next run re-sends everything inside the
  lookback window.
