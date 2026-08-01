#!/usr/bin/env python3
"""
Cyber Digest Bot
Fetches RSS/Atom feeds, filters to the last N hours, dedupes against state/seen.json,
scores by keyword relevance, and pushes a categorized digest to Telegram.

Required env:
    TELEGRAM_BOT_TOKEN   token from @BotFather
    TELEGRAM_CHAT_ID     channel id (-100...) or your user id

Optional env:
    LOOKBACK_HOURS       default 13   (12h cycle + 1h overlap)
    MAX_PER_CATEGORY     default 8
    MAX_PER_FEED         default 3
    MIN_SCORE            default 0    (raise to 2 or 3 to get only high-signal items)
    SEND_EMPTY           default 0    (1 = send a "nothing new" message)
    DRY_RUN              default 0    (1 = print to stdout, do not call Telegram)
"""

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
import yaml

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
FEEDS_FILE = ROOT / "feeds.yaml"
STATE_FILE = ROOT / "state" / "seen.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "13"))
MAX_PER_CATEGORY = int(os.environ.get("MAX_PER_CATEGORY", "8"))
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "3"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "0"))
SEND_EMPTY = os.environ.get("SEND_EMPTY", "0") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

SEEN_LIMIT = 6000          # how many link hashes we remember
TG_SAFE_LEN = 3800         # Telegram hard limit is 4096; leave headroom
HTTP_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 CyberDigestBot/1.0"
)

# Title/summary keywords that push an item up the list
HIGH_VALUE = {
    3: [
        "0-day", "zero-day", "zeroday", "actively exploited", "in the wild",
        "kev catalog", "emergency patch", "unauthenticated rce", "wormable",
    ],
    2: [
        "rce", "remote code execution", "privilege escalation", "auth bypass",
        "authentication bypass", "sql injection", "critical vulnerability",
        "exploit", "proof of concept", "poc", "cvss 9", "cvss 10",
        "supply chain", "backdoor", "ransomware", "data breach",
    ],
    1: [
        "cve-", "released", "release", "new version", "update", "patch",
        "open source", "open-source", "tool", "framework", "bug bounty",
        "writeup", "write-up", "bypass", "malware", "apt", "phishing",
        "kernel", "firmware", "hardening", "detection", "sigma rule",
    ],
}

# Downrank obvious filler
NOISE = [
    "sponsored", "webinar", "podcast", "partner content", "advertisement",
    "top 10 best", "buyer's guide", "black friday", "discount code",
]

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load_feeds() -> dict:
    with FEEDS_FILE.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("categories", {})


def load_seen() -> set:
    if not STATE_FILE.exists():
        return set()
    try:
        with STATE_FILE.open(encoding="utf-8") as fh:
            return set(json.load(fh).get("seen", []))
    except (json.JSONDecodeError, OSError):
        log("seen.json unreadable, starting fresh")
        return set()


def save_seen(seen: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(seen)[-SEEN_LIMIT:]
    with STATE_FILE.open("w", encoding="utf-8") as fh:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                   "seen": trimmed}, fh, indent=0)


def normalize_link(link: str) -> str:
    """Strip tracking params so the same article from two runs hashes identically."""
    try:
        parts = urlparse(link.strip())
        query = [(k, v) for k, v in parse_qsl(parts.query)
                 if k.lower() not in TRACKING_PARAMS]
        clean = parts._replace(query=urlencode(query), fragment="")
        return urlunparse(clean).rstrip("/")
    except ValueError:
        return link.strip()


def item_hash(link: str, title: str) -> str:
    basis = normalize_link(link) or title.strip().lower()
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:16]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        stamp = entry.get(key)
        if stamp:
            try:
                return datetime(*stamp[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def score_item(title: str, summary: str) -> int:
    blob = f"{title} {summary}".lower()
    score = 0
    for weight, words in HIGH_VALUE.items():
        for word in words:
            if word in blob:
                score += weight
                break          # one hit per weight tier, avoids keyword stuffing
    for word in NOISE:
        if word in blob:
            score -= 3
            break
    return score


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_feed(url: str) -> list:
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT,
                            headers={"User-Agent": USER_AGENT,
                                     "Accept": "application/rss+xml, application/atom+xml, */*"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log(f"  FAIL {url} -> {exc.__class__.__name__}")
        return []

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        log(f"  BAD XML {url}")
        return []
    return parsed.entries


def collect(categories: dict, seen: set) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    results: dict[str, list] = {}
    fresh_hashes: set[str] = set()

    for cat_name, cfg in categories.items():
        label = cfg.get("label", cat_name)
        bucket = []

        for feed in cfg.get("feeds", []):
            source = feed.get("name", "?")
            url = feed.get("url", "")
            if not url:
                continue
            log(f"{label} :: {source}")
            entries = fetch_feed(url)
            taken = 0

            for entry in entries:
                if taken >= MAX_PER_FEED:
                    break
                link = entry.get("link", "")
                title = strip_html(entry.get("title", ""))
                if not link or not title:
                    continue

                key = item_hash(link, title)
                if key in seen or key in fresh_hashes:
                    continue

                stamp = entry_datetime(entry)
                if stamp and stamp < cutoff:
                    continue          # too old
                # if stamp is None we keep it; dedupe by hash protects us

                summary = strip_html(entry.get("summary", ""))[:400]
                score = score_item(title, summary) + int(feed.get("boost", 0))
                if score < MIN_SCORE:
                    continue

                bucket.append({
                    "title": title,
                    "link": normalize_link(link),
                    "source": source,
                    "score": score,
                    "when": stamp,
                })
                fresh_hashes.add(key)
                taken += 1

            time.sleep(0.4)     # be polite, avoid rate limits

        bucket.sort(key=lambda i: (-i["score"],
                                   -(i["when"].timestamp() if i["when"] else 0)))
        if bucket:
            results[label] = bucket[:MAX_PER_CATEGORY]

    seen.update(fresh_hashes)
    return results


# --------------------------------------------------------------------------- #
# Rendering + sending
# --------------------------------------------------------------------------- #

def esc(text: str) -> str:
    return html.escape(text, quote=False)


def build_messages(results: dict) -> list[str]:
    now = datetime.now(timezone.utc) + timedelta(hours=3)   # Beirut
    header = (f"<b>CYBER DIGEST</b>\n"
              f"<code>{now:%Y-%m-%d %H:%M}</code> Beirut\n"
              f"{'-' * 26}")
    chunks, current = [], header

    for label, items in results.items():
        block = f"\n\n<b>{esc(label)}</b>\n"
        for item in items:
            line = (f"• <a href=\"{esc(item['link'])}\">{esc(item['title'][:170])}</a>\n"
                    f"  <i>{esc(item['source'])}</i>\n")
            if len(current) + len(block) + len(line) > TG_SAFE_LEN:
                chunks.append(current + block)
                current, block = "", f"<b>{esc(label)}</b> (cont.)\n"
            block += line
        if len(current) + len(block) > TG_SAFE_LEN:
            chunks.append(current)
            current = block
        else:
            current += block

    if current.strip():
        chunks.append(current)
    return chunks


def send(text: str) -> None:
    if DRY_RUN:
        print("\n----- MESSAGE -----\n" + text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(4):
        resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return
        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 5)
            log(f"  429, sleeping {wait}s")
            time.sleep(wait + 1)
            continue
        log(f"  Telegram {resp.status_code}: {resp.text[:300]}")
        time.sleep(3)
    raise RuntimeError("Telegram send failed after retries")


# --------------------------------------------------------------------------- #

def main() -> int:
    if not DRY_RUN and (not BOT_TOKEN or not CHAT_ID):
        log("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return 1

    categories = load_feeds()
    seen = load_seen()
    log(f"loaded {len(seen)} seen hashes, {len(categories)} categories")

    results = collect(categories, seen)
    total = sum(len(v) for v in results.values())
    log(f"collected {total} new items")

    if total == 0:
        if SEND_EMPTY:
            send("<b>CYBER DIGEST</b>\nNo new items in the last "
                 f"{LOOKBACK_HOURS}h.")
        save_seen(seen)
        return 0

    for i, chunk in enumerate(build_messages(results), 1):
        send(chunk)
        log(f"sent chunk {i} ({len(chunk)} chars)")
        time.sleep(1.2)

    save_seen(seen)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
