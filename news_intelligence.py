from __future__ import annotations

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp


NEWS_CACHE_SECONDS = 300
NEWS_CACHE: dict[str, Any] = {"timestamp": 0.0, "data": None}
CONTACT = os.getenv("NEWS_CONTACT_EMAIL", "operator@jansignalbot.local").strip()
FEEDS = (
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Federal Reserve Monetary Policy", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("SEC", "https://www.sec.gov/news/pressreleases.rss"),
)

CRYPTO_TERMS = ("crypto", "bitcoin", "ether", "digital asset", "blockchain", "stablecoin", "token", "etf")
BULLISH_TERMS = ("approve", "approval", "clarity", "innovation", "rescind", "easing", "rate cut", "liquidity support")
BEARISH_TERMS = ("charges", "charged", "enforcement", "fraud", "lawsuit", "sanction", "rate hike", "restrictive", "inflation risk")


def _text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def _classify(source: str, title: str, summary: str) -> tuple[str, int, bool]:
    text = f"{title} {summary}".lower()
    crypto_relevant = any(term in text for term in CRYPTO_TERMS)
    macro_relevant = source.startswith("Federal Reserve") and any(
        term in text for term in ("monetary", "interest rate", "fomc", "inflation", "liquidity")
    )
    relevant = crypto_relevant or macro_relevant
    bullish = sum(term in text for term in BULLISH_TERMS)
    bearish = sum(term in text for term in BEARISH_TERMS)
    if not relevant or bullish == bearish:
        return "NEUTRAL", 0, relevant
    if bullish > bearish:
        return "BULLISH", min(3, bullish), True
    return "BEARISH", -min(3, bearish), True


async def _fetch_feed(source: str, url: str) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
    headers = {
        "Accept": "application/rss+xml, application/xml, text/xml",
        "User-Agent": f"JanCryptoSignalBot/2.0 {CONTACT}",
    }
    last_error: Exception | None = None
    body = ""
    successful = False
    for attempt in range(2):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as response:
                    body = await response.text()
                    if response.status != 200:
                        raise RuntimeError(f"{source} HTTP {response.status}: {body[:200]}")
            successful = True
            break
        except Exception as error:
            last_error = error
            if attempt == 0:
                await asyncio.sleep(1)
    if not successful:
        raise last_error or RuntimeError(f"{source} returned no feed data")
    root = ET.fromstring(body)
    items: list[dict[str, Any]] = []
    for item in root.findall(".//item")[:12]:
        title = _text(item, "title")
        link = _text(item, "link")
        summary = _text(item, "description")
        published_text = _text(item, "pubDate") or _text(item, "date")
        try:
            published = parsedate_to_datetime(published_text)
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published_at = published.astimezone(timezone.utc)
        except (TypeError, ValueError):
            published_at = datetime.now(timezone.utc)
        label, score, relevant = _classify(source, title, summary)
        items.append({
            "id": link or f"{source}:{title}",
            "source": source,
            "title": title,
            "link": link,
            "published_at": published_at,
            "label": label,
            "score": score,
            "relevant": relevant,
        })
    return items


async def fetch_news_intelligence(force: bool = False) -> dict[str, Any]:
    if not force and NEWS_CACHE["data"] is not None and time.time() - NEWS_CACHE["timestamp"] < NEWS_CACHE_SECONDS:
        return dict(NEWS_CACHE["data"])
    results = await asyncio.gather(*(_fetch_feed(source, url) for source, url in FEEDS), return_exceptions=True)
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for (source, _), result in zip(FEEDS, results):
        if isinstance(result, Exception):
            errors.append(f"{source}: {type(result).__name__}")
        else:
            items.extend(result)
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item["id"])
        existing = deduplicated.get(item_id)
        if existing is None or item["source"] == "Federal Reserve Monetary Policy":
            deduplicated[item_id] = item
    items = list(deduplicated.values())
    items.sort(key=lambda item: item["published_at"], reverse=True)
    relevant = [item for item in items if item["relevant"]]
    now = datetime.now(timezone.utc)
    recent = [item for item in relevant if (now - item["published_at"]).total_seconds() <= 24 * 3600]
    score = max(-6, min(6, sum(int(item["score"]) for item in recent)))
    label = "BULLISH" if score >= 2 else "BEARISH" if score <= -2 else "NEUTRAL"
    data = {"items": items[:20], "relevant_items": relevant[:8], "recent_items": recent, "score": score, "label": label, "live": bool(items), "errors": errors}
    NEWS_CACHE.update(timestamp=time.time(), data=data)
    return dict(data)


def build_news_message(data: dict[str, Any]) -> str:
    icon = "🟢" if data.get("label") == "BULLISH" else "🔴" if data.get("label") == "BEARISH" else "🟡"
    lines = ["📰 NEWS INTELLIGENCE", "", f"{icon} 24h bias: {data.get('label', 'NEUTRAL')} ({int(data.get('score', 0)):+d}; capped at ±6)", "Sources: Federal Reserve and SEC official RSS", "Truth Social: unavailable (official endpoint blocks access)", "X: unavailable without paid API access", "", "RELEVANT HEADLINES"]
    items = data.get("relevant_items", [])[:6]
    if not items:
        lines.append("• No relevant official headline detected.")
    for item in items:
        item_icon = "🟢" if item["label"] == "BULLISH" else "🔴" if item["label"] == "BEARISH" else "🟡"
        date_text = item["published_at"].astimezone(timezone.utc).strftime("%b %-d")
        lines.append(f"{item_icon} [{item['source']}, {date_text}] {item['title']}")
    if data.get("errors"):
        lines.extend(["", f"Feed warnings: {', '.join(data['errors'])}"])
    lines.extend(["", "Headline classification is conservative and never creates an entry by itself."])
    return "\n".join(lines)
