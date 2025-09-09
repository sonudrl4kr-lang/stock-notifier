# telegram_market_notifier_once.py
import os
import json
import re
from pathlib import Path
from datetime import datetime
from dateutil import tz, parser as dtparser
import requests
import feedparser
import yfinance as yf

# Config from env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLL_FEEDS = os.getenv("RSS_FEEDS", "")
SEEN_DB = os.getenv("SEEN_DB", "seen.json")
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "8"))
SUMMARY_WORDS = int(os.getenv("SUMMARY_WORDS", "18"))
TRANSLATE_TARGET = os.getenv("TRANSLATE_TARGET", "hi")
TICKERS = os.getenv("TICKERS", "RELIANCE.NS,AAPL,TSLA").split(",")  # Stock tickers from env, default to some stocks

# Keywords for filtering
DEFAULT_KEYWORDS = [
    "NSE", "BSE", "RBI", "RESULT", "RESULTS", "EARNINGS",
    "CORPORATE", "NIFTY", "SENSEX", "QUARTERLY", "IPO",
    "MERGER", "ACQUISITION"
]
KEYWORDS_ENV = os.getenv("KEYWORDS", "")
if KEYWORDS_ENV.strip():
    KEYWORDS = [k.strip() for k in KEYWORDS_ENV.split(",") if k.strip()]
else:
    KEYWORDS = DEFAULT_KEYWORDS

escaped = [re.escape(k) for k in KEYWORDS if k]
pattern = re.compile(r"(" + r"|".join(escaped) + r")", flags=re.IGNORECASE) if escaped else None

# Updated top news feeds (reliable for stock market and corporate news)
DEFAULT_FEEDS = [
    "https://feeds.reuters.com/reuters/marketsNews",  # Global markets
    "https://feeds.reuters.com/reuters/INbusinessNews",  # India business
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",  # ET Markets
    "https://www.moneycontrol.com/rss/buzzingstocks.xml",  # Moneycontrol Buzzing Stocks
    "https://www.moneycontrol.com/rss/marketreports.xml",  # Moneycontrol Market Reports
]
if POLL_FEEDS.strip():
    RSS_FEEDS = [u.strip() for u in POLL_FEEDS.split(",") if u.strip()]
else:
    RSS_FEEDS = DEFAULT_FEEDS

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in repo secrets / env.")
    raise SystemExit(1)

SEEN_DB_PATH = Path(SEEN_DB)
if not SEEN_DB_PATH.exists():
    SEEN_DB_PATH.write_text(json.dumps({"seen": []}, indent=2))

def load_seen():
    try:
        return set(json.loads(SEEN_DB_PATH.read_text()).get("seen", []))
    except Exception:
        return set()

def save_seen(seen_set):
    SEEN_DB_PATH.write_text(json.dumps({"seen": list(seen_set)}, indent=2))

def safe_translate(text, target=TRANSLATE_TARGET):
    if not text:
        return ""
    try:
        params = {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
        resp = requests.get("https://translate.googleapis.com/translate_a/single", params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        translated = "".join([chunk[0] for chunk in data[0] if chunk and chunk[0]])
        return translated
    except Exception:
        return text

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print("Telegram send error:", e)
        return False

def fetch_from_rss(feed_url):
    try:
        feed = feedparser.parse(feed_url)
    except Exception as e:
        print("RSS parse error:", feed_url, e)
        return []
    items = []
    for entry in feed.entries:
        uid = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title")
        title = entry.get("title", "")
        summary = entry.get("summary") or entry.get("description") or ""
        link = entry.get("link", "")
        src = feed.feed.get("title", "") or ""
        items.append({"id": uid, "title": title, "summary": summary, "link": link, "source": src, "published": entry.get("published", "")})
    return items

def fetch_from_yfinance(tickers):
    items = []
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            articles = stock.news  # Fetch news from yfinance
            for article in articles[:3]:  # Limit to 3 news per stock
                uid = article.get("uuid") or article.get("link") or article.get("title")
                title = article.get("title", "")
                summary = article.get("publisher", "")  # yfinance news has no summary, use publisher as context
                link = article.get("link", "")
                src = article.get("publisher", "Yahoo Finance")
                published = article.get("providerPublishTime", "")
                if published:
                    published = datetime.fromtimestamp(published, tz=tz.gettz("Asia/Kolkata")).isoformat()
                items.append({"id": uid, "title": title, "summary": summary, "link": link, "source": src, "published": published})
        except Exception as e:
            print(f"yfinance error for {ticker}:", e)
    return items

def parse_time_safe(s):
    try:
        return dtparser.parse(s)
    except Exception:
        return datetime.now(tz=tz.gettz("Asia/Kolkata"))

def short_summary(text, words=SUMMARY_WORDS):
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", "", text)
    toks = re.split(r"\s+", t)
    short = " ".join(toks[:words])
    if len(toks) > words:
        short += "…"
    return short

def build_msg(item):
    title_en = item.get("title", "")
    summ_en = short_summary(item.get("summary", ""))
    title_hi = safe_translate(title_en)
    summ_hi = safe_translate(summ_en) if summ_en else ""
    src = item.get("source", "")
    link = item.get("link", "")
    msg = f"<b>{escape_html(title_hi)}</b>"
    if src:
        msg += f"\n<i>{escape_html(src)}</i>"
    if summ_hi:
        msg += f"\n{escape_html(summ_hi)}"
    if link:
        msg += f'\n\n<a href="{escape_html(link)}">🔗 पढ़ें</a>'
    return msg

def escape_html(t):
    if not t:
        return ""
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

def matches_keywords(item):
    if not pattern:
        return True
    text = " ".join([str(item.get("title", "")), str(item.get("summary", "")), str(item.get("source", ""))])
    return bool(pattern.search(text))

def main():
    seen = load_seen()
    new_items = []
    
    # Fetch from RSS feeds
    for feed in RSS_FEEDS:
        for it in fetch_from_rss(feed):
            if not it.get("id"):
                it["id"] = (it.get("link") or it.get("title"))[:300]
            if it["id"] in seen:
                continue
            if matches_keywords(it):
                new_items.append(it)
    
    # Fetch from yfinance
    for it in fetch_from_yfinance(TICKERS):
        if not it.get("id"):
            it["id"] = (it.get("link") or it.get("title"))[:300]
        if it["id"] in seen:
            continue
        if matches_keywords(it):
            new_items.append(it)
    
    # Sort items by publication time
    new_items.sort(key=lambda x: parse_time_safe(x.get("published", "")), reverse=False)
    
    # Send messages
    sent = 0
    for item in new_items:
        if sent >= MAX_PER_RUN:
            break
        msg = build_msg(item)
        ok = send_telegram_message(msg)
        if ok:
            seen.add(item["id"])
            sent += 1
            print("Sent:", item.get("title", "")[:80])
        else:
            print("Failed to send:", item.get("title", "")[:80])
    
    save_seen(seen)
    print("Run complete. Sent:", sent)

if __name__ == "__main__":
    main()
