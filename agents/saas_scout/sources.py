"""Phase 2: listing ingestion.

Neither Acquire.com nor the Chrome Web Store publishes an official API, so
three sources are offered:

* ``FileSource``: a JSON array of normalised listings (the stable path; feed it
  the output of an Apify actor, a CSV export, or your own scraper).
* ``AcquireSitemapSource``: reads ``app.acquire.com/sitemap.xml`` and parses the
  JSON-LD block on each public listing page. Best effort, no login.
* ``ChromeWebStoreSource``: fetches extension detail pages for the IDs you
  configure and parses users, last-updated date and version from the HTML.

Every source yields the same normalised dict (see ``normalise_listing``).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from ..common import http

ACQUIRE_SITEMAP_URL = "https://app.acquire.com/sitemap.xml"
CHROME_WEBSTORE_DETAIL_URL = "https://chromewebstore.google.com/detail/{extension_id}"

FIELDS = (
    "asset_id", "name", "category", "source", "source_url", "asking_price", "claimed_users",
    "last_updated_date", "domain", "description", "seller_contact_url", "manifest_version",
)


def normalise_listing(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = (raw.get("name") or raw.get("title") or "").strip()
    source_url = (raw.get("source_url") or raw.get("url") or "").strip()
    if not name and not source_url:
        return None
    asset_id = str(raw.get("asset_id") or raw.get("id") or hashlib.sha1((source_url or name).encode("utf-8")).hexdigest()[:12])
    domain = raw.get("domain") or _domain_from_url(raw.get("website") or raw.get("homepage") or "")
    listing = {
        "asset_id": asset_id,
        "name": name or source_url,
        "category": str(raw.get("category") or raw.get("type") or "unknown").strip(),
        "source": raw.get("source") or "file",
        "source_url": source_url,
        "asking_price": _to_number(raw.get("asking_price") if raw.get("asking_price") is not None else raw.get("price")),
        "claimed_users": _to_int(raw.get("claimed_users") if raw.get("claimed_users") is not None else raw.get("users")),
        "last_updated_date": raw.get("last_updated_date") or raw.get("last_updated") or raw.get("updated"),
        "domain": domain,
        "description": str(raw.get("description") or "").strip()[:2000],
        "seller_contact_url": raw.get("seller_contact_url") or raw.get("contact_url") or source_url,
        "manifest_version": raw.get("manifest_version"),
    }
    if "_fixture_enrichment" in raw:
        listing["_fixture_enrichment"] = raw["_fixture_enrichment"]
    return listing


def _to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d.]", "", str(value))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_number(value)
    return int(number) if number is not None else None


def _domain_from_url(url: str) -> str | None:
    if not url:
        return None
    if "//" not in url:
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


class FileSource:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def fetch(self) -> list[dict[str, Any]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("listings") or data.get("items") or data.get("data") or []
        out = []
        for raw in data:
            listing = normalise_listing(raw)
            if listing:
                out.append(listing)
        return out


# ---------------------------------------------------------------- Acquire.com

LISTING_URL_PATTERN = re.compile(r"https://app\.acquire\.com/startup/[^<\s]+")
JSON_LD_PATTERN = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I)


class AcquireSitemapSource:
    def __init__(self, *, max_listings: int = 150, timeout: float = 30.0, fetch=None):
        self.max_listings = max_listings
        self.timeout = timeout
        self._fetch = fetch or (lambda url: http.request("GET", url, timeout=self.timeout, headers={"Accept": "text/html,application/xml"}).text)

    def listing_urls(self) -> list[str]:
        xml = self._fetch(ACQUIRE_SITEMAP_URL)
        urls = list(dict.fromkeys(LISTING_URL_PATTERN.findall(xml)))
        return urls[: self.max_listings]

    def fetch(self) -> list[dict[str, Any]]:
        out = []
        for url in self.listing_urls():
            try:
                page = self._fetch(url)
            except http.HttpError:
                continue
            listing = parse_acquire_listing_html(page, url)
            if listing:
                out.append(listing)
        return out


def parse_acquire_listing_html(page: str, url: str) -> dict[str, Any] | None:
    """Pull name, price, description and category from the page's JSON-LD."""
    product: dict[str, Any] | None = None
    for block in JSON_LD_PATTERN.findall(page):
        try:
            data = json.loads(html.unescape(block).strip())
        except json.JSONDecodeError:
            continue
        for node in _iter_ld_nodes(data):
            if str(node.get("@type", "")).lower() in {"product", "offer", "listing", "webpage"}:
                product = node
                break
        if product:
            break
    if not product:
        title = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        if not title:
            return None
        product = {"name": html.unescape(title.group(1)).strip()}
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price") if isinstance(offers, dict) else None
    raw = {
        "id": "acq_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10],
        "name": product.get("name"),
        "category": product.get("category") or "saas",
        "source": "acquire",
        "source_url": url,
        "asking_price": price,
        "description": product.get("description"),
        "website": product.get("url") if product.get("url") and "acquire.com" not in str(product.get("url")) else None,
        "last_updated": product.get("dateModified") or product.get("datePublished"),
        "seller_contact_url": url,
    }
    return normalise_listing(raw)


def _iter_ld_nodes(data: Any) -> Iterable[dict[str, Any]]:
    if isinstance(data, dict):
        yield data
        for value in data.values():
            yield from _iter_ld_nodes(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)


# ------------------------------------------------------------ Chrome Web Store

USERS_PATTERN = re.compile(r"([\d,.]+)\s*(K|M)?\+?\s+users", re.I)
UPDATED_PATTERN = re.compile(r"Updated.{0,120}?([A-Z][a-z]+ \d{1,2}, \d{4})", re.S)
VERSION_PATTERN = re.compile(r"Version.{0,120}?(\d[\w.\-]*)", re.S)
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.S | re.I)
OG_TITLE_PATTERN = re.compile(r"property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)", re.I)
DESCRIPTION_PATTERN = re.compile(r"name=[\"']description[\"'][^>]*content=[\"']([^\"']+)", re.I)


class ChromeWebStoreSource:
    def __init__(self, extension_ids: Iterable[str], *, timeout: float = 30.0, fetch=None):
        self.extension_ids = [e.strip() for e in extension_ids if e.strip()]
        self.timeout = timeout
        self._fetch = fetch or (lambda url: http.request("GET", url, timeout=self.timeout, headers={"Accept": "text/html"}).text)

    def fetch(self) -> list[dict[str, Any]]:
        out = []
        for ext in self.extension_ids:
            url = ext if ext.startswith("http") else CHROME_WEBSTORE_DETAIL_URL.format(extension_id=ext)
            try:
                page = self._fetch(url)
            except http.HttpError:
                continue
            listing = parse_chrome_webstore_html(page, url)
            if listing:
                out.append(listing)
        return out


def parse_chrome_webstore_html(page: str, url: str) -> dict[str, Any] | None:
    title_match = OG_TITLE_PATTERN.search(page) or TITLE_PATTERN.search(page)
    name = html.unescape(title_match.group(1)).replace(" - Chrome Web Store", "").strip() if title_match else None
    users = None
    users_match = USERS_PATTERN.search(page)
    if users_match:
        number = _to_number(users_match.group(1)) or 0
        suffix = (users_match.group(2) or "").upper()
        users = int(number * {"K": 1_000, "M": 1_000_000}.get(suffix, 1))
    updated = UPDATED_PATTERN.search(page)
    version = VERSION_PATTERN.search(page)
    description = DESCRIPTION_PATTERN.search(page)
    ext_id = url.rstrip("/").split("/")[-1]
    raw = {
        "id": f"ext_{ext_id[:12]}",
        "name": name,
        "category": "chrome-extension",
        "source": "chromewebstore",
        "source_url": url,
        "asking_price": None,  # not listed for sale; treat as outreach target
        "claimed_users": users,
        "last_updated": updated.group(1) if updated else None,
        "description": html.unescape(description.group(1)) if description else None,
        "manifest_version": None,
        "seller_contact_url": url,
    }
    if version:
        raw["version"] = version.group(1)
    return normalise_listing(raw)
