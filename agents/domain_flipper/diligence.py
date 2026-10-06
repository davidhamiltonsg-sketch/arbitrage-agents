"""Due-diligence helpers for shortlisted domains: Wayback history and a trademark screen.

Both run server-side (the GitHub runner has open internet) and their results
are embedded in the digest and the exported JSON so the dashboard can show
them without making its own network calls.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import Any

from ..common import http
from ..common.cache import SEVEN_DAYS, Cache

CDX_URL = "https://web.archive.org/cdx/search/cdx"
# GoDaddy's appraisal (GoValue) is free with a production API key from developer.godaddy.com.
GODADDY_API_BASE = "https://api.godaddy.com"

# Marks whose presence in a name is a near-certain conflict. A cheap screen,
# not clearance: always check the registries linked below before buying.
FAMOUS_MARKS: tuple[str, ...] = (
    "google", "youtube", "gmail", "android", "apple", "iphone", "ipad", "macbook", "microsoft",
    "windows", "xbox", "office365", "amazon", "kindle", "alexa", "facebook", "instagram",
    "whatsapp", "meta", "twitter", "tiktok", "snapchat", "netflix", "disney", "pixar", "marvel",
    "nike", "adidas", "puma", "reebok", "coca", "cocacola", "pepsi", "starbucks", "mcdonald",
    "walmart", "target", "costco", "ikea", "lego", "tesla", "toyota", "honda", "bmw", "mercedes",
    "ford", "ferrari", "porsche", "uber", "lyft", "airbnb", "paypal", "stripe", "visa",
    "mastercard", "amex", "samsung", "sony", "nintendo", "playstation", "adobe", "photoshop",
    "oracle", "salesforce", "shopify", "openai", "chatgpt", "anthropic", "claude", "spotify",
    "linkedin", "reddit", "pinterest", "zoom", "slack", "dropbox", "github", "intel", "nvidia",
    "amd", "cisco", "ibm", "hp", "dell", "lenovo", "huawei", "xiaomi", "rolex", "gucci", "prada",
    "chanel", "louisvuitton", "hermes", "burberry", "zara", "hm", "uniqlo", "fedex", "ups", "dhl",
    "marriott", "hilton", "booking", "expedia", "tripadvisor", "ebay", "etsy", "alibaba",
    "aliexpress", "wechat", "baidu", "yahoo", "bing", "verizon", "comcast", "vodafone",
)


def wayback_summary(domain: str, *, timeout: float = 20.0, fetch=None) -> dict[str, Any]:
    """Summarise a domain's Wayback Machine history via the public CDX API (no key)."""
    params = {
        "url": domain,
        "output": "json",
        "fl": "timestamp,statuscode,mimetype",
        "filter": "statuscode:200",
        "collapse": "timestamp:6",  # one row per month
        "limit": "2000",
    }
    base = {
        "status": "unknown",
        "first_year": None,
        "last_year": None,
        "snapshot_months": 0,
        "years_active": 0,
        "latest_url": None,
        "timeline_url": f"https://web.archive.org/web/*/{domain}",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        if fetch is not None:
            payload = fetch(domain)
        else:
            payload = http.get_json(CDX_URL, params=params, timeout=timeout, retries=0)
    except Exception as exc:  # never sink a run over archive availability
        return {**base, "status": "error", "error": str(exc)[:200]}
    return {**base, **summarise_cdx(domain, payload)}


def summarise_cdx(domain: str, payload: Any) -> dict[str, Any]:
    rows = [r for r in (payload or []) if isinstance(r, list) and r and r[0] != "timestamp"]
    if not rows:
        return {"status": "none", "snapshot_months": 0, "years_active": 0}
    stamps = sorted(str(r[0]) for r in rows if str(r[0])[:4].isdigit())
    if not stamps:
        return {"status": "none", "snapshot_months": 0, "years_active": 0}
    years = sorted({int(s[:4]) for s in stamps})
    return {
        "status": "ok",
        "first_year": years[0],
        "last_year": years[-1],
        "snapshot_months": len(stamps),
        "years_active": len(years),
        "latest_url": f"https://web.archive.org/web/{stamps[-1]}/{domain}",
    }


def trademark_screen(domain: str) -> dict[str, Any]:
    """Cheap in-process screen plus prefilled registry searches."""
    sld = domain.split(".")[0].lower()
    # Marks under 4 letters only count as an exact match ("hp", "ups", "amd" occur inside ordinary words).
    flags = [m for m in FAMOUS_MARKS if (m == sld) or (len(m) >= 4 and m in sld)]
    if sld in FAMOUS_MARKS:
        risk = "high"
    elif any(len(m) >= 5 or sld.startswith(m) for m in flags):
        # A long mark anywhere, or a short one leading the name ("nikeoutlet"), is a likely conflict.
        risk = "high"
    elif flags:
        # A 4-letter mark inside or ending an ordinary word ("bestnikeshoes", "climbing" has "bing") needs a human look.
        risk = "medium"
    else:
        risk = "low"
    q = urllib.parse.quote(sld, safe="")
    return {
        "term": sld,
        "risk": risk,
        "flags": flags,
        "summary": (
            "contains a famous mark: " + ", ".join(flags) if flags else "no famous-mark match in the built-in list"
        ),
        "links": {
            "uspto": f"https://tmsearch.uspto.gov/search/search-results?query={q}",
            "wipo": f"https://branddb.wipo.int/en/quicksearch?by=brandName&v={q}",
            "euipo": f"https://euipo.europa.eu/eSearch/#basic/1+1+1+1/100+100+100+100/{q}",
        },
    }


def sample_diligence(domain: str) -> dict[str, Any]:
    """Deterministic placeholder for dry runs (no network)."""
    seed = sum(ord(c) for c in domain)
    first = 2008 + seed % 10
    last = min(2025, first + seed % 7)
    return {
        "wayback": {
            "status": "sample",
            "first_year": first,
            "last_year": last,
            "snapshot_months": 3 + seed % 40,
            "years_active": last - first + 1,
            "latest_url": f"https://web.archive.org/web/{last}0101000000/{domain}",
            "timeline_url": f"https://web.archive.org/web/*/{domain}",
            "checked_at": None,
        },
        "trademark": trademark_screen(domain),
    }


def describe_wayback(w: dict[str, Any]) -> str:
    status = w.get("status")
    if status in ("ok", "sample"):
        span = f"{w['first_year']}" if w.get("first_year") == w.get("last_year") else f"{w.get('first_year')}–{w.get('last_year')}"
        return f"archived {span}, {w.get('snapshot_months', 0)} monthly snapshots"
    if status == "none":
        return "no Wayback snapshots (never archived or always blocked)"
    if status == "error":
        return "Wayback check failed, open the timeline manually"
    return "Wayback not checked"


def godaddy_auth_header(key: str | None, secret: str | None) -> str | None:
    if not key or not secret:
        return None
    return f"sso-key {key.strip()}:{secret.strip()}"


def parse_appraisal(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce a GoDaddy appraisal response to the fields the digest shows."""
    value = payload.get("govalue")
    try:
        value = int(round(float(value))) if value is not None else None
    except (TypeError, ValueError):
        value = None
    comps = []
    for comp in payload.get("comparable_sales") or []:
        if not isinstance(comp, dict) or not comp.get("domain"):
            continue
        try:
            price = int(round(float(comp.get("price") or 0)))
        except (TypeError, ValueError):
            price = 0
        comps.append({"domain": str(comp["domain"]), "price": price, "year": comp.get("year")})
    reasons = []
    for reason in payload.get("reasons") or []:
        if isinstance(reason, dict):
            text = reason.get("description") or reason.get("type")
            if text:
                reasons.append(str(text))
        elif isinstance(reason, str):
            reasons.append(reason)
    return {"status": "ok" if value is not None else "none", "value": value, "currency": "USD", "comparables": comps[:5], "reasons": reasons[:5]}


def appraise(
    domain: str,
    auth_header: str,
    *,
    cache: Cache | None = None,
    ttl_seconds: float = SEVEN_DAYS,
    base_url: str = GODADDY_API_BASE,
    timeout: float = 20.0,
    fetch=None,
) -> dict[str, Any]:
    """GoDaddy GoValue appraisal with comparable sales; never raises."""
    url = f"{base_url.rstrip('/')}/v1/appraisal/{urllib.parse.quote(domain, safe='')}"
    base = {"status": "unknown", "source": "godaddy", "value": None, "currency": "USD", "comparables": [], "reasons": [],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def call() -> dict[str, Any]:
        payload = fetch(url) if fetch is not None else http.get_json(url, headers={"Authorization": auth_header, "Accept": "application/json"}, timeout=timeout, retries=1)
        if not isinstance(payload, dict):
            raise ValueError("unexpected appraisal payload")
        return parse_appraisal(payload)

    try:
        parsed = cache.remember("godaddy:appraisal", domain, call, ttl_seconds) if cache else call()
    except http.HttpError as exc:
        if exc.status in (401, 403):
            return {**base, "status": "denied", "error": "GoDaddy rejected the API key (needs a production key with appraisal access)"}
        if exc.status in (404, 422):
            return {**base, "status": "none", "error": "GoDaddy has no appraisal for this name"}
        return {**base, "status": "error", "error": str(exc)[:200]}
    except (ValueError, TypeError, KeyError) as exc:
        return {**base, "status": "error", "error": str(exc)[:200]}
    return {**base, **parsed}


def sample_appraisal(domain: str) -> dict[str, Any]:
    seed = sum(ord(c) for c in domain)
    value = 400 + (seed % 23) * 100
    return {"status": "sample", "source": "sample", "value": value, "currency": "USD",
            "comparables": [{"domain": f"{domain.split('.')[0][:4]}hub.com", "price": value + 250, "year": 2024}], "reasons": [], "checked_at": None}


def describe_appraisal(a: dict[str, Any] | None) -> str:
    status = (a or {}).get("status")
    if status in ("ok", "sample") and a.get("value") is not None:
        comps = a.get("comparables") or []
        text = f"GoDaddy GoValue ${a['value']:,}" + (" (sample)" if status == "sample" else "")
        if comps:
            text += " · comps: " + ", ".join(f"{c['domain']} ${c['price']:,}" + (f" ({c['year']})" if c.get("year") else "") for c in comps[:3])
        return text
    if status == "none":
        return "no GoDaddy appraisal for this name"
    if status == "denied":
        return "GoDaddy appraisal denied: check GODADDY_API_KEY/SECRET"
    if status == "error":
        return "GoDaddy appraisal failed on the runner"
    return "no appraisal (add GODADDY_API_KEY and GODADDY_API_SECRET)"
