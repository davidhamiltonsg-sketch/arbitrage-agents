"""Phase 4b: is the dropped domain still free to register? (RDAP, no key)

Drop-catchers re-register the best names within minutes of the drop, so a
"dropped" feed entry is not a guarantee. Every registry publishes RDAP:
``404`` means the name is not in the registry (free to register), ``200``
returns the registration with its status list. The RDAP server for each TLD
comes from the IANA bootstrap file, cached for a week.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ..common import http
from ..common.cache import SEVEN_DAYS, Cache

IANA_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
# Known bases so the two biggest TLDs work even if the bootstrap fetch fails.
KNOWN_RDAP_BASES = {
    "com": "https://rdap.verisign.com/com/v1/",
    "net": "https://rdap.verisign.com/net/v1/",
}

STATUS_AVAILABLE = "available"
STATUS_TAKEN = "taken"
STATUS_PENDING_DELETE = "pending-delete"
STATUS_UNKNOWN = "unknown"
STATUS_UNCHECKED = "unchecked"
STATUS_SAMPLE = "sample"

BLOCKING_STATUSES = (STATUS_TAKEN,)


def rdap_bases(bootstrap: Any) -> dict[str, str]:
    """Map every TLD in an IANA bootstrap payload to its first https RDAP base URL."""
    out: dict[str, str] = {}
    for entry in (bootstrap or {}).get("services") or []:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        tlds, urls = entry
        https = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
        base = (https or [u for u in urls if isinstance(u, str)] or [None])[0]
        if not base:
            continue
        for tld in tlds:
            out[str(tld).lower().lstrip(".")] = base if base.endswith("/") else base + "/"
    return out


def load_rdap_bases(cache: Cache | None = None, *, timeout: float = 20.0) -> dict[str, str]:
    def call() -> dict[str, str]:
        return rdap_bases(http.get_json(IANA_BOOTSTRAP_URL, timeout=timeout, retries=1))

    try:
        fetched = cache.remember("rdap", "bootstrap", call, SEVEN_DAYS) if cache else call()
    except (http.HttpError, ValueError, TypeError, KeyError):
        fetched = {}
    return {**KNOWN_RDAP_BASES, **fetched}


def _event(payload: dict[str, Any], action: str) -> str | None:
    for event in payload.get("events") or []:
        if isinstance(event, dict) and str(event.get("eventAction", "")).lower() == action:
            return event.get("eventDate")
    return None


def _registrar(payload: dict[str, Any]) -> str | None:
    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict) or "registrar" not in [str(r).lower() for r in entity.get("roles") or []]:
            continue
        vcard = entity.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) == 2:
            for prop in vcard[1]:
                if isinstance(prop, list) and prop and prop[0] == "fn" and len(prop) >= 4:
                    return str(prop[3])
        for pid in entity.get("publicIds") or []:
            if isinstance(pid, dict) and pid.get("identifier"):
                return f"IANA registrar {pid['identifier']}"
    return None


def parse_rdap(payload: dict[str, Any]) -> dict[str, Any]:
    """Interpret a 200 RDAP domain object."""
    statuses = [str(s).lower() for s in payload.get("status") or []]
    pending = any("pending delete" in s or "redemption" in s for s in statuses)
    return {
        "status": STATUS_PENDING_DELETE if pending else STATUS_TAKEN,
        "registrar": _registrar(payload),
        "registered": _event(payload, "registration"),
        "expires": _event(payload, "expiration"),
        "statuses": statuses,
    }


def check_availability(
    domain: str,
    *,
    bases: dict[str, str] | None = None,
    timeout: float = 15.0,
    fetch: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Return ``{status, source, checked_at, ...}``; never raises."""
    tld = domain.rsplit(".", 1)[-1].lower()
    base = (bases or KNOWN_RDAP_BASES).get(tld)
    out: dict[str, Any] = {"status": STATUS_UNKNOWN, "source": "rdap", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not base and fetch is None:
        return {**out, "note": f"no RDAP server published for .{tld}"}
    url = f"{base}domain/{domain}"
    try:
        payload = fetch(url) if fetch is not None else http.get_json(url, timeout=timeout, retries=1)
    except http.HttpError as exc:
        if exc.status == 404:
            return {**out, "status": STATUS_AVAILABLE, "server": base}
        return {**out, "server": base, "note": str(exc)[:160]}
    except (ValueError, TypeError, KeyError) as exc:
        return {**out, "server": base, "note": f"unreadable RDAP answer: {exc}"[:160]}
    if not isinstance(payload, dict):
        return {**out, "server": base, "note": "unreadable RDAP answer"}
    return {**out, "server": base, **parse_rdap(payload)}


def sample_availability(domain: str) -> dict[str, Any]:
    return {"status": STATUS_SAMPLE, "source": "sample", "checked_at": None}


def describe(a: dict[str, Any] | None) -> str:
    status = (a or {}).get("status")
    if status in (STATUS_AVAILABLE, STATUS_SAMPLE):
        return "available to register (RDAP)" if status == STATUS_AVAILABLE else "available (sample)"
    if status == STATUS_PENDING_DELETE:
        return "still pending delete at the registry; drops within days, place a backorder"
    if status == STATUS_TAKEN:
        who = a.get("registrar")
        return f"already re-registered ({who})" if who else "already re-registered"
    if status == STATUS_UNCHECKED:
        return "not checked (outside the per-run cap)"
    note = (a or {}).get("note")
    return f"availability unknown ({note})" if note else "availability unknown"
