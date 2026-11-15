"""Verified-link RSS evidence collection with strict attribution and bounded input sizes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import re
import socket
import time
from typing import Callable, Protocol
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from config.settings import Settings, get_settings
from research.models import EvidenceItem


class ResearchSourceError(RuntimeError):
    """A configured source could not be retrieved or parsed safely."""


@dataclass(frozen=True)
class FeedDefinition:
    url: str
    kind: str


class ResearchContentProvider(Protocol):
    def company_announcements(self, symbol: str, company_name: str) -> list[EvidenceItem]: ...
    def market_news(self, symbol: str, company_name: str) -> list[EvidenceItem]: ...


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)


def _plain_text(value: str, limit: int = 5000) -> str:
    parser = _TextExtractor()
    parser.feed(value[:limit * 2])
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:limit]


def _local_host(host: str) -> bool:
    if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ResearchSourceError("Configured feed hostname could not be resolved") from exc
        return any(not ipaddress.ip_address(item[4][0]).is_global for item in resolved)


def validate_feed_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ResearchSourceError("Research feed URLs must use HTTPS and cannot contain credentials")
    if _local_host(parsed.hostname):
        raise ResearchSourceError("Research feed hostname resolves to a non-public address")


def _field_text(element: ElementTree.Element, names: tuple[str, ...]) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    # Atom elements use a namespace; match by local name as a fallback.
    for found in element.iter():
        local = found.tag.rsplit("}", 1)[-1]
        if local in names and found.text:
            return found.text.strip()
    return ""


def _entry_link(element: ElementTree.Element) -> str:
    direct = _field_text(element, ("link", "guid"))
    if direct.startswith("https://"):
        return direct
    for found in element.iter():
        if found.tag.rsplit("}", 1)[-1] == "link":
            href = found.attrib.get("href", "")
            if href.startswith("https://"):
                return href
    return ""


def _parse_published(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


class RSSResearchContentProvider:
    """Fetch user-configured RSS/Atom feeds, retaining publisher links and timestamps."""

    MAX_FEED_BYTES = 1_000_000
    MAX_ITEMS_PER_FEED = 100

    def __init__(self, *, market_feeds: tuple[str, ...] | list[str],
                 announcement_feeds: dict[str, list[str]] | None = None,
                 getter: Callable[..., requests.Response] = requests.get,
                 cache_seconds: float = 300, timeout: float = 8.0) -> None:
        if cache_seconds < 0 or timeout <= 0:
            raise ValueError("Feed cache duration and timeout are invalid")
        self.market_feeds = tuple(market_feeds)
        self.announcement_feeds = {symbol.upper(): tuple(urls)
                                   for symbol, urls in (announcement_feeds or {}).items()}
        self.getter = getter
        self.cache_seconds = cache_seconds
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list[EvidenceItem]]] = {}
        self.source_errors: list[str] = []

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RSSResearchContentProvider":
        config = settings or get_settings()
        return cls(market_feeds=config.research_market_news_feeds,
                   announcement_feeds=config.research_announcement_feeds,
                   timeout=config.upstox_api_timeout_seconds)

    def company_announcements(self, symbol: str, company_name: str) -> list[EvidenceItem]:
        symbol = symbol.strip().upper()
        feeds = self.announcement_feeds.get(symbol, ())
        items = self._fetch_feeds(feeds, "company_announcement")
        return [item for item in items if self._matches(item, symbol, company_name)]

    def market_news(self, symbol: str, company_name: str) -> list[EvidenceItem]:
        items = self._fetch_feeds(self.market_feeds, "market_news")
        broad_market_terms = ("market", "nifty", "sensex", "nse", "bse", "rbi", "sebi", "rupee",
                              "equity", "stocks", "inflation", "interest rate", "economy", "bond")
        symbol = symbol.strip().upper()
        company_words = {word.lower() for word in re.findall(r"[a-zA-Z]{4,}", company_name)}
        results = []
        for item in items:
            corpus = f"{item.title} {item.summary}".lower()
            if symbol.lower() in corpus or any(word in corpus for word in company_words) or any(term in corpus for term in broad_market_terms):
                results.append(item)
        return results

    def _fetch_feeds(self, urls: tuple[str, ...], kind: str) -> list[EvidenceItem]:
        self.source_errors = []
        result: list[EvidenceItem] = []
        for url in urls:
            try:
                result.extend(self._fetch_feed(url, kind))
            except ResearchSourceError as exc:
                self.source_errors.append(f"{url}: {exc}")
        unique: dict[str, EvidenceItem] = {item.evidence_id: item for item in result}
        return sorted(unique.values(), key=lambda item: item.published_at or item.retrieved_at, reverse=True)

    def _fetch_feed(self, url: str, kind: str) -> list[EvidenceItem]:
        validate_feed_url(url)
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return list(cached[1])
        try:
            response = self.getter(url, timeout=self.timeout, allow_redirects=False,
                                   headers={"User-Agent": "TradePilotAI/1.0 (+local research)", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"})
        except requests.RequestException as exc:
            raise ResearchSourceError("Feed request failed") from exc
        if response.status_code != 200:
            raise ResearchSourceError(f"Feed returned HTTP {response.status_code}")
        content = response.content
        if len(content) > self.MAX_FEED_BYTES:
            raise ResearchSourceError("Feed exceeded the configured size limit")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ResearchSourceError("Feed was not valid RSS/Atom XML") from exc
        host = urlparse(url).hostname or "unknown source"
        official = host.lower().endswith(("nseindia.com", "bseindia.com"))
        retrieved = datetime.now(timezone.utc)
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
        items: list[EvidenceItem] = []
        for entry in entries[:self.MAX_ITEMS_PER_FEED]:
            title = _plain_text(_field_text(entry, ("title",)), 500)
            link = _entry_link(entry)
            summary = _plain_text(_field_text(entry, ("description", "summary", "content")))
            if not title or not link:
                continue
            link_info = urlparse(link)
            if link_info.scheme != "https" or not link_info.hostname:
                continue
            published = _parse_published(_field_text(entry, ("pubDate", "published", "updated", "date")))
            digest = sha256(f"{kind}|{link}|{published}|{title}".encode("utf-8")).hexdigest()[:20]
            items.append(EvidenceItem(digest, kind, title, summary, link, host, published, retrieved, official))
        self._cache[url] = (time.monotonic(), items)
        return list(items)

    @staticmethod
    def _matches(item: EvidenceItem, symbol: str, company_name: str) -> bool:
        corpus = f"{item.title} {item.summary}".lower()
        symbol_match = re.search(rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])", corpus)
        company_words = [word.lower() for word in re.findall(r"[a-zA-Z]{4,}", company_name)
                         if word.lower() not in {"limited", "ltd", "industries", "company"}]
        return bool(symbol_match or any(word in corpus for word in company_words))


class EmptyResearchContentProvider:
    """Explicit no-content adapter; makes no assumptions about news or announcements."""

    source_errors: list[str] = []

    def company_announcements(self, symbol: str, company_name: str) -> list[EvidenceItem]:
        return []

    def market_news(self, symbol: str, company_name: str) -> list[EvidenceItem]:
        return []
