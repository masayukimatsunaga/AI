#!/usr/bin/env python3
"""Build the same-origin daily USD/JPY reference snapshot used by index.html."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "daily.json"
JST = timezone(timedelta(hours=9))
SOURCE_NAME = "外為どっとコム マネ育チャンネル"
CATEGORY_URL = (
    "https://www.gaitame.com/media/archive/category/"
    + urllib.parse.quote("外為トゥデイ")
)
RSS_URL = (
    "https://www.gaitame.com/media/rss/category/"
    + urllib.parse.quote("外為トゥデイ")
)
ORDER_GUIDE_URL = "https://www.gaitame.com/markets/order/det/"
ORDER_CATEGORY_URL = (
    "https://www.gaitame.com/media/archive/category/"
    + urllib.parse.quote("今日の注文情報")
)
ORDER_RSS_URL = (
    "https://www.gaitame.com/media/rss/category/"
    + urllib.parse.quote("今日の注文情報")
)


class TextExtractor(HTMLParser):
    """Turn a small HTML fragment into readable plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "li", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "li", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(fragment: str) -> str:
    parser = TextExtractor()
    parser.feed(html.unescape(fragment or ""))
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; USDJPYDashboard/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep(2**attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def rss_datetime(value: str) -> str:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST).isoformat()


def created_datetime(body: str) -> str:
    text = plain_text(body)
    match = re.search(
        r"作成日時\s*[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})時(\d{1,2})分",
        text,
    )
    if not match:
        match = re.search(
            r"作成日時\s*[：:]\s*(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})",
            text,
        )
    if not match:
        return ""
    year, month, day, hour, minute = map(int, match.groups())
    return datetime(year, month, day, hour, minute, tzinfo=JST).isoformat()


def extract_heading(body: str, tag: str, element_id: str) -> str:
    match = re.search(
        rf"<{tag}\b[^>]*\bid=[\"']{re.escape(element_id)}[\"'][^>]*>(.*?)</{tag}>",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    return plain_text(match.group(1)) if match else ""


def extract_factors(body: str) -> list[str]:
    factors: list[str] = []
    for fragment in re.findall(
        r"<h3\b[^>]*\bid=[\"'][123][\"'][^>]*>(.*?)</h3>",
        body,
        re.IGNORECASE | re.DOTALL,
    ):
        value = re.sub(r"^[（(][123１-３][）)]\s*[：:]?\s*", "", plain_text(fragment))
        if value and value not in factors:
            factors.append(value)
    return factors[:3]


def extract_events(body: str) -> list[dict[str, str]]:
    section = re.search(
        r"<h2\b[^>]*\bid=[\"']event[\"'][^>]*>.*?</h2>(.*?)(?:<div\b[^>]*class=[\"'][^\"']*g-ads|<h2\b)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if not section:
        return []
    lines = plain_text(section.group(1)).splitlines()
    rows: list[dict[str, str]] = []
    for line in lines:
        value = re.sub(r"\s+", " ", line).strip()
        if not value or re.match(r"^\d{2}/\d{2}\s*[（(]", value):
            continue
        level = "high" if "◎" in value else "medium" if "〇" in value else "low"
        rows.append({"text": value, "importance": level})
    important = [row for row in rows if row["importance"] != "low"]
    return (important or rows)[:6]


def extract_order_book_image(body: str) -> str:
    section = re.search(
        r"<h2\b[^>]*\bid=[\"']tool[\"'][^>]*>.*?</h2>(.*?)(?:<h2\b[^>]*\bid=[\"']range[\"']|<h2\b)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if not section:
        return ""
    image_match = re.search(
        r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
        section.group(1),
        re.IGNORECASE | re.DOTALL,
    )
    if not image_match:
        return ""
    image_url = html.unescape(image_match.group(1)).strip()
    parsed = urllib.parse.urlparse(image_url)
    allowed_hosts = {"cdn-ak.f.st-hatena.com", "cdn.image.st-hatena.com"}
    if parsed.scheme != "https" or parsed.netloc not in allowed_hosts:
        raise RuntimeError(f"Unexpected order-book image URL: {image_url}")
    return image_url


def extract_order_report_summary(body: str) -> str:
    section = re.search(
        r"<h2\b[^>]*>\s*ドル円の上値[・･]?下値の注文状況\s*</h2>\s*<p\b[^>]*>(.*?)</p>",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    return plain_text(section.group(1)) if section else ""


def extract_order_report_image(body: str) -> str:
    candidates = re.findall(r"<img\b[^>]*>", body, re.IGNORECASE | re.DOTALL)
    for tag in candidates:
        alt_match = re.search(r"\balt=[\"']([^\"']*)[\"']", tag, re.IGNORECASE)
        alt = html.unescape(alt_match.group(1)) if alt_match else ""
        if "注文情報" not in alt or "ドル" not in alt or "現在" not in alt:
            continue
        src_match = re.search(r"\bsrc=[\"']([^\"']+)[\"']", tag, re.IGNORECASE)
        if not src_match:
            continue
        image_url = html.unescape(src_match.group(1)).strip()
        parsed = urllib.parse.urlparse(image_url)
        if parsed.scheme == "https" and parsed.netloc in {
            "cdn-ak.f.st-hatena.com",
            "cdn.image.st-hatena.com",
        }:
            return image_url
    return ""


def extract_order_levels(summary: str, side: str) -> list[float]:
    if side == "sell":
        pattern = r"(?:上側|上値)[^。]*(?:売り(?:オーダー)?)"
    else:
        pattern = r"(?:下側|下値)[^。]*(?:買い(?:オーダー)?)"
    match = re.search(pattern, summary)
    if not match:
        return []
    values = {
        float(value)
        for value in re.findall(r"(?<!\d)(\d{3}(?:\.\d{1,3})?)\s*円", match.group(0))
    }
    return sorted(values)


def extract_order_unit(body: str) -> str:
    text = plain_text(body)
    match = re.search(r"1メモリ\s*[＝=]\s*([0-9,，]+)\s*通貨", text)
    if not match:
        return ""
    amount = match.group(1).replace("，", ",")
    return f"1メモリ＝{amount}通貨"


def headline_from_title(title: str) -> str:
    match = re.search(r"ドル/円今日の見通し\s*(?:｜|\|)\s*(.*?)」\s*外為", title)
    return match.group(1).strip() if match else title.strip()


def parse_report(xml_text: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)
    selected: ET.Element | None = None
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if "外為どっとコム トゥデイ" in title and "ドル/円" in title:
            selected = item
            break
    if selected is None:
        raise RuntimeError("No 外為どっとコム トゥデイ USD/JPY report was found")

    source_title = (selected.findtext("title") or "").strip()
    link = (selected.findtext("link") or "").strip()
    parsed_link = urllib.parse.urlparse(link)
    if parsed_link.scheme != "https" or parsed_link.netloc != "www.gaitame.com":
        raise RuntimeError(f"Unexpected report URL: {link}")
    if not parsed_link.path.startswith("/media/entry/"):
        raise RuntimeError(f"Unexpected report path: {link}")

    body = selected.findtext("description") or ""
    text = plain_text(body)
    range_match = re.search(
        r"ドル[/／]円\s*[：:]\s*([0-9]+(?:\.[0-9]+)?)\s*[～〜]\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )
    forecast_range = None
    if range_match:
        forecast_range = {
            "low": float(range_match.group(1)),
            "high": float(range_match.group(2)),
        }

    published_at = rss_datetime((selected.findtext("pubDate") or "").strip())
    if datetime.now(JST) - datetime.fromisoformat(published_at) > timedelta(days=10):
        raise RuntimeError("The newest daily report is more than 10 days old")

    return {
        "headline": headline_from_title(source_title),
        "sourceTitle": source_title,
        "url": link,
        "publishedAt": published_at,
        "createdAt": created_datetime(body),
        "forecastRange": forecast_range,
        "outlook": re.sub(r"^ドル/円の見通し\s*[：:]\s*", "", extract_heading(body, "h2", "outlook")),
        "factors": extract_factors(body),
        "events": extract_events(body),
        "orderBookImage": extract_order_book_image(body),
        "orderBookGuideUrl": ORDER_GUIDE_URL,
    }


def parse_order_report(xml_text: str) -> dict[str, object]:
    root = ET.fromstring(xml_text)
    selected: ET.Element | None = None
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if "今日の外為注文情報" in title and "ドル円" in title:
            selected = item
            break
    if selected is None:
        raise RuntimeError("No USD/JPY order report was found")

    source_title = (selected.findtext("title") or "").strip()
    link = (selected.findtext("link") or "").strip()
    parsed_link = urllib.parse.urlparse(link)
    if (
        parsed_link.scheme != "https"
        or parsed_link.netloc != "www.gaitame.com"
        or not parsed_link.path.startswith("/media/entry/")
    ):
        raise RuntimeError(f"Unexpected order report URL: {link}")

    body = selected.findtext("description") or ""
    summary = extract_order_report_summary(body)
    published_at = rss_datetime((selected.findtext("pubDate") or "").strip())
    if datetime.now(JST) - datetime.fromisoformat(published_at) > timedelta(days=10):
        raise RuntimeError("The newest order report is more than 10 days old")

    headline = re.sub(r"\s*今日の外為注文情報.*$", "", source_title).strip()
    return {
        "headline": headline,
        "sourceTitle": source_title,
        "url": link,
        "publishedAt": published_at,
        "createdAt": created_datetime(body),
        "summary": summary,
        "sellLevels": extract_order_levels(summary, "sell"),
        "buyLevels": extract_order_levels(summary, "buy"),
        "orderBookImage": extract_order_report_image(body),
        "unitText": extract_order_unit(body),
        "orderBookGuideUrl": ORDER_GUIDE_URL,
    }


def current_content() -> dict[str, object] | None:
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.pop("updatedAt", None)
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return None


def previous_order_report() -> dict[str, object] | None:
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        value = payload.get("orderReport")
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def main() -> None:
    try:
        order_report = parse_order_report(fetch_text(ORDER_RSS_URL))
    except Exception as error:
        order_report = previous_order_report()
        if order_report is None:
            raise
        print(f"Keeping previous order report: {error}")

    content = {
        "source": {
            "name": SOURCE_NAME,
            "categoryUrl": CATEGORY_URL,
            "cadence": "平日毎朝8:30〜9:00頃",
            "orderCategoryUrl": ORDER_CATEGORY_URL,
            "orderCadence": "平日17:00頃",
        },
        "report": parse_report(fetch_text(RSS_URL)),
        "orderReport": order_report,
    }
    if content == current_content():
        print("No daily report changes")
        return

    payload = {
        "updatedAt": datetime.now(JST).isoformat(timespec="seconds"),
        **content,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {OUTPUT.name} from {payload['report']['publishedAt']}")


if __name__ == "__main__":
    main()
