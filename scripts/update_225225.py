#!/usr/bin/env python3
"""Build the 225225.jp market context used by the USD/JPY dashboard."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "market_context.json"
JST = timezone(timedelta(hours=9))
NEWS_FEEDS = (
    "https://nikkei225jp.com/_data/_nfsDATA/rss/News_9.js",
    "https://nikkei225jp.com/_data/_nfsDATA/rss/News_ALL1.js",
)
FX_PAGE = "https://nikkei225jp.com/fx/"
NEWS_PAGE = "https://nikkei225jp.com/news/"
SCHEDULE_PAGE = "https://nikkei225jp.com/schedule/"
RELEVANT_NEWS = re.compile(
    r"(?:ドル[/／・\s-]*円|USD.?JPY|円相場|円安|円高|為替介入|外国為替|外為|"
    r"FOMC|FRB|連邦準備|米(?:国)?(?:長期)?金利|米金利|日銀|植田総裁|"
    r"米.*(?:CPI|PPI|雇用|小売売上|GDP|ISM)|日本.*(?:GDP|CPI|物価))",
    re.IGNORECASE,
)
OTHER_PAIR_OUTLOOK = re.compile(r"^(?:英ポンド|ユーロ|豪ドル|NZドル|カナダドル)週間見通し")
STRONGER_NORMAL = re.compile(
    r"CPI|PPI|PCE|GDP|小売売上|雇用者数|平均時給|ISM|PMI|鉱工業生産|設備稼働|景気指数|賃金|物価"
)
INVERSE_INDICATOR = re.compile(r"失業率|失業保険|新規失業")


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(fragment: str) -> str:
    parser = TextExtractor()
    parser.feed(html.unescape(fragment or ""))
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; USDJPYDashboard/1.0)",
            "Accept": "text/html, text/javascript, */*",
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


def feed_datetime(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M").replace(tzinfo=JST).isoformat()
    except ValueError:
        return ""


def unescape_javascript(value: str) -> str:
    value = re.sub(r"\\(['\"\\/])", r"\1", value)
    return value.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")


def parse_news_feed(text: str) -> list[dict[str, str]]:
    encoded_rows = re.findall(r"News\[n\]='((?:\\.|[^'])*)';n\+\+", text)
    items: list[dict[str, str]] = []
    for encoded in encoded_rows:
        fields = unescape_javascript(encoded).split("__")
        if len(fields) < 8:
            continue
        title = plain_text(fields[7])
        link = fields[6].strip()
        if (
            OTHER_PAIR_OUTLOOK.search(title)
            or not RELEVANT_NEWS.search(title)
            or not link.startswith(("https://", "http://"))
        ):
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "publishedAt": feed_datetime(fields[1].strip()),
                "source": fields[5].strip() or "225225.jp掲載ニュース",
            }
        )
    return items


def deduplicate_news(items: list[dict[str, str]], limit: int = 12) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    used: set[str] = set()
    for item in sorted(items, key=lambda row: row.get("publishedAt", ""), reverse=True):
        key = re.sub(r"\s+", "", item["title"])
        if key in used:
            continue
        used.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def extract_cell(row: str, class_name: str) -> str:
    match = re.search(
        rf"<td\b[^>]*class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>(.*?)</td>",
        row,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        match = re.search(
            rf"<td\b[^>]*class={re.escape(class_name)}\b[^>]*>(.*?)</td>",
            row,
            re.IGNORECASE | re.DOTALL,
        )
    return plain_text(match.group(1)) if match else ""


def closest_event_datetime(month: int, day: int, time_text: str, now: datetime) -> datetime | None:
    match = re.match(r"(\d{1,2}):(\d{2})", time_text)
    if not match:
        return None
    hour, minute = map(int, match.groups())
    day_offset, hour = divmod(hour, 24)
    candidates: list[datetime] = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            candidates.append(datetime(year, month, day, hour, minute, tzinfo=JST) + timedelta(days=day_offset))
        except ValueError:
            continue
    return min(candidates, key=lambda value: abs((value - now).total_seconds())) if candidates else None


def parse_schedule(page: str, now: datetime) -> list[dict[str, object]]:
    table_match = re.search(
        r"<table\b[^>]*\bid=[\"']?SihyoT[\"']?[^>]*>(.*?)</table>",
        page,
        re.IGNORECASE | re.DOTALL,
    )
    if not table_match:
        raise RuntimeError("225225.jp economic indicator table was not found")
    current_month = current_day = None
    events: list[dict[str, object]] = []
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_match.group(1), re.IGNORECASE | re.DOTALL):
        date_text = extract_cell(row, "date")
        date_match = re.search(r"(\d{1,2})/(\d{1,2})", date_text)
        if date_match:
            current_month, current_day = map(int, date_match.groups())
            continue
        if current_month is None or current_day is None:
            continue
        time_text = extract_cell(row, "time")
        title = extract_cell(row, "event")
        if not time_text or not title:
            continue
        flag = re.search(r"flag1-([a-z]{2})", row, re.IGNORECASE)
        country_code = flag.group(1).lower() if flag else ""
        priority_match = re.search(r"priority(\d)", row)
        priority = int(priority_match.group(1)) if priority_match else extract_cell(row, "priority").count("★")
        event_time = closest_event_datetime(current_month, current_day, time_text, now)
        if event_time is None:
            continue
        events.append(
            {
                "scheduledAt": event_time.isoformat(),
                "dateLabel": f"{current_month}/{current_day}",
                "timeLabel": time_text,
                "countryCode": country_code,
                "country": {"jp": "日本", "us": "米国"}.get(country_code, country_code.upper() or "その他"),
                "importance": priority,
                "title": title,
                "result": extract_cell(row, "result"),
                "forecast": extract_cell(row, "expectation"),
                "previous": extract_cell(row, "last"),
                "url": SCHEDULE_PAGE,
            }
        )
    return events


def numeric_value(value: str) -> float | None:
    cleaned = value.replace(",", "").replace("−", "-").replace("▲", "-")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    return float(match.group()) if match else None


def classify_news(news: list[dict[str, str]]) -> tuple[int, list[str], list[str], list[str]]:
    score = 0
    up: list[str] = []
    down: list[str] = []
    neutral: list[str] = []
    for item in news[:8]:
        title = item["title"]
        item_up: list[str] = []
        item_down: list[str] = []
        if re.search(r"ドル高|米.*(?:利上げ|金利上昇)|FRB.*タカ|FOMC.*タカ", title) or (
            "円安" in title
            and "円安是正" not in title
            and "円安牽制" not in title
            and "為替介入" not in title
        ):
            item_up.append(title)
        if re.search(r"円高|ドル安|為替介入|米.*(?:利下げ|金利低下)|FRB.*ハト|FOMC.*ハト", title):
            item_down.append(title)
        if re.search(r"日銀.*(?:利上げ|引き締め|タカ)|植田.*(?:利上げ|引き締め)", title):
            item_down.append(title)
        if re.search(r"日銀.*(?:利下げ|緩和|ハト)", title):
            item_up.append(title)
        if item_up:
            score += 1
            if title not in up:
                up.append(title)
        if item_down:
            score -= 1
            if title not in down:
                down.append(title)
        if not item_up and not item_down and len(neutral) < 3:
            neutral.append(title)
    return score, up, down, neutral


def indicator_surprise(event: dict[str, object]) -> int:
    title = str(event.get("title", ""))
    country = str(event.get("countryCode", ""))
    result = numeric_value(str(event.get("result", "")))
    forecast = numeric_value(str(event.get("forecast", "")))
    if result is None or forecast is None or not (STRONGER_NORMAL.search(title) or INVERSE_INDICATOR.search(title)):
        return 0
    delta = result - forecast
    if abs(delta) < 1e-9:
        return 0
    stronger = -1 if INVERSE_INDICATOR.search(title) else 1
    surprise = stronger if delta > 0 else -stronger
    if country == "us":
        return surprise
    if country == "jp":
        return -surprise
    return 0


def summarize_fundamentals(news: list[dict[str, str]], recent: list[dict[str, object]]) -> dict[str, object]:
    news_score, up, down, neutral = classify_news(news)
    indicator_score = 0
    for event in recent:
        surprise = indicator_surprise(event)
        if not surprise:
            continue
        indicator_score += surprise
        text = (
            f"{event['country']} {event['title']}：結果 {event['result']}／予想 {event['forecast']}"
        )
        (up if surprise > 0 else down).append(text)
    score = max(-5, min(5, news_score + indicator_score))
    if score >= 2:
        label = "ドル高・円安材料が優勢"
    elif score <= -2:
        label = "ドル安・円高材料が優勢"
    else:
        label = "材料は拮抗"
    return {
        "score": score,
        "label": label,
        "upFactors": up[:5],
        "downFactors": down[:5],
        "neutralFactors": neutral[:3],
        "method": "225225.jp掲載ニュースの政策・金利語句と、日米主要指標の結果対市場予想を機械整理",
    }


def load_existing() -> dict[str, object]:
    try:
        payload = json.loads(OUTPUT.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def comparable(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "updatedAt"}


def main() -> None:
    now = datetime.now(JST)
    collected: list[dict[str, str]] = []
    successful_feeds: list[str] = []
    errors: list[str] = []
    for url in NEWS_FEEDS:
        try:
            collected.extend(parse_news_feed(fetch_text(url)))
            successful_feeds.append(url)
        except Exception as error:
            errors.append(f"ニュース取得失敗: {error}")
    news = deduplicate_news(collected)
    if not news:
        raise RuntimeError("No relevant 225225.jp news was available")

    try:
        schedule = parse_schedule(fetch_text(FX_PAGE), now)
    except Exception as error:
        raise RuntimeError(f"225225.jp schedule unavailable: {error}") from error

    recent = [
        event for event in schedule
        if event["countryCode"] in {"jp", "us"}
        and event["importance"] >= 3
        and event["result"]
        and now - timedelta(days=4) <= datetime.fromisoformat(str(event["scheduledAt"])) <= now
    ][-10:]
    upcoming = [
        event for event in schedule
        if event["countryCode"] in {"jp", "us"}
        and event["importance"] >= 3
        and now <= datetime.fromisoformat(str(event["scheduledAt"])) <= now + timedelta(days=8)
    ][:18]
    if not upcoming:
        errors.append("8日以内の日米重要指標を取得できませんでした")

    payload: dict[str, object] = {
        "updatedAt": now.isoformat(timespec="seconds"),
        "source": {
            "name": "225225.jp / 日経225先物 リアルタイムCME SGX",
            "newsUrl": NEWS_PAGE,
            "fxUrl": "https://225225.jp/4fx/",
            "scheduleUrl": SCHEDULE_PAGE,
            "feeds": successful_feeds,
        },
        "news": news,
        "recentIndicators": recent,
        "upcomingIndicators": upcoming,
        "fundamental": summarize_fundamentals(news, recent),
        "warnings": errors,
        "disclaimer": "公開情報の機械整理であり、売買推奨や将来の価格保証ではありません。",
    }

    existing = load_existing()
    existing_time = datetime.fromisoformat(str(existing.get("updatedAt", ""))) if existing.get("updatedAt") else None
    recently_checked = bool(existing_time and now - existing_time < timedelta(hours=2))
    if existing and comparable(existing) == comparable(payload) and recently_checked:
        print("225225.jp context is unchanged and was checked within two hours")
        return
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Updated {OUTPUT.name}: {len(news)} news, {len(recent)} recent indicators, "
        f"{len(upcoming)} upcoming indicators"
    )


if __name__ == "__main__":
    main()
