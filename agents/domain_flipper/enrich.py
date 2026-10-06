"""Phase 4: backlink authority via DataForSEO Backlinks Summary (live).

DataForSEO's ``rank`` is a 0 to 1000 domain-rank scale, not the 0 to 100
Ahrefs DR the cookbook's thresholds assume. It is scaled to ``dr = rank / 10``
so the published gates (``dr >= 10``, ``referring_domains >= 5``) keep their
intended meaning. Override via DR_SCALE_DIVISOR if you plug in another vendor.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from ..common import http
from ..common.cache import SEVEN_DAYS, Cache

DATAFORSEO_URL = "https://api.dataforseo.com/v3/backlinks/summary/live"


class EnrichmentError(Exception):
    pass


@dataclass
class AuthorityGate:
    min_dr: float = 10
    min_referring_domains: int = 5


def basic_auth_header(login: str | None, password: str | None, raw_token: str | None = None) -> str:
    """Build the ``Authorization: Basic`` value from login/password or a prebuilt base64 token."""
    if raw_token:
        token = raw_token.strip()
        return token if token.lower().startswith("basic ") else f"Basic {token}"
    if not login or not password:
        raise EnrichmentError("DataForSEO credentials missing: set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD, or DATAFORSEO_AUTH")
    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def parse_summary(payload: dict[str, Any], *, dr_divisor: float = 10.0) -> dict[str, Any]:
    if payload.get("status_code") not in (None, 20000):
        raise EnrichmentError(f"DataForSEO error {payload.get('status_code')}: {payload.get('status_message')}")
    tasks = payload.get("tasks") or []
    if not tasks:
        raise EnrichmentError("DataForSEO returned no tasks")
    task = tasks[0]
    if task.get("status_code") not in (None, 20000):
        raise EnrichmentError(f"DataForSEO task error {task.get('status_code')}: {task.get('status_message')}")
    results = task.get("result") or []
    result = results[0] if results else {}
    rank = float(result.get("rank") or 0)
    return {
        "rank": rank,
        "dr": round(rank / dr_divisor, 1) if dr_divisor else rank,
        "referring_domains": int(result.get("referring_domains") or 0),
        "total_backlinks": int(result.get("backlinks") or 0),
        "spam_score": result.get("backlinks_spam_score"),
        "first_seen": result.get("first_seen"),
    }


def fetch_backlink_summary(
    domain: str,
    auth_header: str,
    *,
    cache: Cache | None = None,
    ttl_seconds: float = SEVEN_DAYS,
    dr_divisor: float = 10.0,
    timeout: float = 60.0,
) -> dict[str, Any]:
    def call() -> dict[str, Any]:
        body = [{"target": domain, "internal_list_limit": 1, "backlinks_status_type": "live"}]
        payload = http.post_json(DATAFORSEO_URL, body, headers={"Authorization": auth_header}, timeout=timeout)
        return parse_summary(payload, dr_divisor=dr_divisor)

    if cache is None:
        return call()
    return cache.remember("dataforseo:summary", domain, call, ttl_seconds)


def synthetic_metrics(domain: str) -> dict[str, Any]:
    """Deterministic pseudo-metrics for dry runs with no fixture metrics attached."""
    digest = hashlib.sha256(domain.encode("utf-8")).digest()
    rank = digest[0] * 2  # 0..510 on the 0..1000 scale
    referring = digest[1] // 2
    return {
        "rank": rank,
        "dr": round(rank / 10, 1),
        "referring_domains": referring,
        "total_backlinks": referring * (1 + digest[2] % 12),
        "spam_score": digest[3] % 30,
        "first_seen": None,
    }


def passes_authority_gate(metrics: dict[str, Any], gate: AuthorityGate) -> bool:
    return float(metrics.get("dr") or 0) >= gate.min_dr and int(metrics.get("referring_domains") or 0) >= gate.min_referring_domains
