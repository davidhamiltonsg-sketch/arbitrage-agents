"""Phase 4: tech stack (BuiltWith) and verified traffic (Similarweb)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..common import http
from ..common.cache import SEVEN_DAYS, Cache

BUILTWITH_URL = "https://api.builtwith.com/v21/api.json"
SIMILARWEB_URL = "https://api.similarweb.com/v1/website/{domain}/total-traffic-and-engagement/visits"


@dataclass
class TrafficGate:
    min_monthly_visits: int = 800
    min_installs: int = 1_200


def parse_builtwith(payload: dict[str, Any], limit: int = 25) -> list[str]:
    names: list[str] = []
    for result in payload.get("Results") or []:
        inner = result.get("Result") or {}
        for path in inner.get("Paths") or []:
            for tech in path.get("Technologies") or []:
                name = tech.get("Name")
                if name and name not in names:
                    names.append(name)
    return names[:limit]


def fetch_tech_stack(domain: str, api_key: str, *, cache: Cache | None = None, ttl_seconds: float = SEVEN_DAYS, timeout: float = 60.0) -> list[str]:
    def call() -> list[str]:
        payload = http.get_json(BUILTWITH_URL, params={"KEY": api_key, "LOOKUP": domain}, timeout=timeout)
        return parse_builtwith(payload)

    if cache is None:
        return call()
    return cache.remember("builtwith", domain, call, ttl_seconds)


def last_full_months(today: date, count: int = 3) -> tuple[str, str]:
    """Return (start, end) as YYYY-MM for the ``count`` months before the current one."""
    year, month = today.year, today.month
    # Similarweb publishes with roughly a one-month lag; end on the previous month.
    month -= 1
    if month == 0:
        month, year = 12, year - 1
    end = f"{year:04d}-{month:02d}"
    month -= count - 1
    while month <= 0:
        month += 12
        year -= 1
    start = f"{year:04d}-{month:02d}"
    return start, end


def parse_similarweb(payload: dict[str, Any]) -> int | None:
    points = payload.get("visits") or []
    values = [float(p.get("visits") or 0) for p in points if isinstance(p, dict)]
    if not values:
        return None
    return int(round(sum(values) / len(values)))


def fetch_monthly_visits(
    domain: str, api_key: str, *, today: date | None = None, cache: Cache | None = None, ttl_seconds: float = SEVEN_DAYS, timeout: float = 60.0
) -> int | None:
    start, end = last_full_months(today or date.today())

    def call() -> int | None:
        payload = http.get_json(
            SIMILARWEB_URL.format(domain=domain),
            params={"api_key": api_key, "start_date": start, "end_date": end, "country": "world", "granularity": "monthly", "main_domain_only": "false"},
            timeout=timeout,
        )
        return parse_similarweb(payload)

    if cache is None:
        return call()
    return cache.remember("similarweb:visits", f"{domain}:{start}:{end}", call, ttl_seconds)


def synthetic_enrichment(listing: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(listing["asset_id"].encode("utf-8")).digest()
    stacks = (["jQuery", "PHP", "MySQL"], ["Vue 2", "Firebase"], ["Vanilla JS", "Chrome Extension MV2"], ["Ruby on Rails", "Heroku"], ["Python Flask", "Bootstrap"])
    return {
        "tech_stack": stacks[digest[0] % len(stacks)],
        "monthly_visits": None if listing.get("domain") is None else 200 + digest[1] * 25,
    }


def passes_traffic_gate(listing: dict[str, Any], gate: TrafficGate) -> bool:
    visits = listing.get("monthly_visits") or 0
    installs = listing.get("claimed_users") or 0
    return visits >= gate.min_monthly_visits or installs >= gate.min_installs
