"""Phase 3: deterministic listing filters (price, staleness, category, footprint)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

DEFAULT_BLOCKED_CATEGORIES: tuple[str, ...] = ("crypto", "gambling", "adult", "vpn", "dating", "casino", "betting")

MONTH_DATE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")


@dataclass
class SaasFilterConfig:
    max_asking_price: float = 10_000
    min_inactive_days: int = 540
    blocked_categories: tuple[str, ...] = DEFAULT_BLOCKED_CATEGORIES
    min_claimed_users: int = 1_000
    require_price: bool = False


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:len(fmt) + 7] if "%f" in fmt else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    match = MONTH_DATE.match(text)
    if match:
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    return None


def check_listing(listing: dict[str, Any], cfg: SaasFilterConfig, today: date) -> str | None:
    price = listing.get("asking_price")
    if price is None:
        if cfg.require_price:
            return "no-price"
    elif price > cfg.max_asking_price:
        return f"price:{price:g}"

    updated = parse_date(listing.get("last_updated_date"))
    if updated is None:
        return "no-last-updated"
    inactive_days = (today - updated).days
    if inactive_days < cfg.min_inactive_days:
        return f"too-recent:{inactive_days}d"

    haystack = f"{listing.get('category', '')} {listing.get('name', '')}".lower()
    for term in cfg.blocked_categories:
        if term and term in haystack:
            return f"category:{term}"

    users = listing.get("claimed_users")
    if users is None or users < cfg.min_claimed_users:
        return f"users:{users}"
    return None


def apply_filters(
    listings: Iterable[dict[str, Any]], cfg: SaasFilterConfig, today: date | None = None
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    today = today or date.today()
    kept: list[dict[str, Any]] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for listing in listings:
        key = listing.get("source_url") or listing["asset_id"]
        if key in seen:
            rejected.append((listing, "duplicate"))
            continue
        seen.add(key)
        reason = check_listing(listing, cfg, today)
        if reason:
            rejected.append((listing, reason))
        else:
            kept.append(listing)
    return kept, rejected
