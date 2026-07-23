#!/usr/bin/env python3
"""Build the same-origin Japanese USD/JPY news snapshot used by index.html."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "news.json"
JST = timezone(timedelta(hours=9))
MARKET_FEED = "https://nikkei225jp.com/_data/_nfsDATA/rss/News_9.js"
GOOGLE_QUERIES = (
    "ドル円 OR 円相場 OR 円安 OR 円高 OR 為替介入",
    "ドル円 日銀 FOMC 米国金利",
)
RELEVANT = re.compile(
    r"(?:米ドル|ドル)[/／・\s-]*円|USD.?JPY|円相場|円安|円高|対ドル|外国為替|外為|為替介入|"
    r"FOMC|FRB|米(?:国)?(?:長期)?金利|日銀.*(?:円|為替|利上げ)|"
    r"(?:円|為替).*(?:日銀|財務相|政府)",
    re.IGNORECASE,
)
USDJPY = re.compile(r"(?:米ドル|ドル)[/／・\s-]*円|USD.?JPY", re.IGNORECASE)
OTHER_CURRENCY = re.compile(
    r"(?:豪ドル|NZドル|カナダドル|香港ドル|台湾ドル|ユーロ|英ポンド|ポンド|ランド)",
    re.IGNORECASE,
)
OTHER_YEN_PAIR = re.compile(
    r"(?:豪ドル|NZドル|カナダドル|香港ドル|台湾ドル|ユーロ|英ポンド|ポンド|ランド).{0,10}円",
    re.IGNORECASE,
)
STOCK_YEN_MOVE = re.compile(r"[0-9０-９][0-9０-９,，]*円(?:高|安)")
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; USDJPYDashboard/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, text/javascript, */*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as error:  # Network failures are retried, then another source is used.
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def feed_datetime(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M").replace(tzinfo=JST).isoformat()
    except ValueError:
        return ""


def rss_datetime(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(JST).isoformat()
    except (TypeError, ValueError):
        return ""


def unescape_javascript(value: str) -> str:
    value = re.sub(r"\\(['\"\\/])", r"\1", value)
    return value.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")


def parse_market_feed(text: str) -> list[dict[str, str]]:
    encoded_rows = re.findall(r"News\[n\]='((?:\\.|[^'])*)';n\+\+", text)
    items: list[dict[str, str]] = []
    for encoded in encoded_rows:
        fields = unescape_javascript(encoded).split("__")
        if len(fields) < 8 or not is_relevant_title(fields[7]):
            continue
        items.append(
            {
                "title": fields[7].strip(),
                "link": fields[6].strip(),
                "pubDate": feed_datetime(fields[1].strip()),
                "source": fields[5].strip(),
            }
        )
    return items


def parse_google_rss(text: str) -> list[dict[str, str]]:
    root = ET.fromstring(text)
    items: list[dict[str, str]] = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        source = (node.findtext("source") or "Google ニュース").strip()
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        items.append(
            {
                "title": title,
                "link": link,
                "pubDate": rss_datetime((node.findtext("pubDate") or "").strip()),
                "source": source,
            }
        )
    return items


def is_relevant_title(title: str) -> bool:
    if STOCK_YEN_MOVE.search(title):
        return False
    if OTHER_YEN_PAIR.search(title) and not USDJPY.search(OTHER_CURRENCY.sub("", title)):
        return False
    return bool(RELEVANT.search(title))


def valid_item(item: dict[str, str]) -> bool:
    title = item.get("title", "")
    return bool(
        JAPANESE.search(title)
        and is_relevant_title(title)
        and item.get("link", "").startswith(("https://", "http://"))
    )


def deduplicate(items: list[dict[str, str]], limit: int = 14) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    used: set[str] = set()
    for item in sorted(items, key=lambda row: row.get("pubDate", ""), reverse=True):
        if not valid_item(item):
            continue
        key = re.sub(r"\s+", "", item["title"])
        if key in used:
            continue
        used.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def load_existing_items() -> list[dict[str, str]]:
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return payload.get("items", []) if isinstance(payload, dict) else []
    except (OSError, json.JSONDecodeError):
        return []


def main() -> None:
    collected: list[dict[str, str]] = []
    successful_sources: list[str] = []

    try:
        collected.extend(parse_market_feed(fetch_text(MARKET_FEED)))
        successful_sources.append("nikkei225jp.com 為替ニュース")
    except Exception as error:
        print(f"Market feed unavailable: {error}")

    for query in GOOGLE_QUERIES:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote_plus(query)
            + "&hl=ja&gl=JP&ceid=JP:ja"
        )
        try:
            collected.extend(parse_google_rss(fetch_text(url)))
            if "Google ニュース" not in successful_sources:
                successful_sources.append("Google ニュース")
        except Exception as error:
            print(f"Google News feed unavailable: {error}")

    items = deduplicate(collected)
    if len(items) < 5:
        raise RuntimeError(f"Only {len(items)} valid Japanese news items were available")

    if items == load_existing_items():
        print(f"No headline changes ({len(items)} items)")
        return

    payload = {
        "updatedAt": datetime.now(JST).isoformat(timespec="seconds"),
        "sources": successful_sources,
        "items": items,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {OUTPUT.name} with {len(items)} Japanese headlines")


if __name__ == "__main__":
    main()
