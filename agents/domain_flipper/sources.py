"""Phase 2: fetch dropped domains from WhoisFreaks (or a local fixture).

The documented daily feed lives at ``files.whoisfreaks.com/v3.1/domains/dropped``
(``apiKey`` required, ``date`` yyyy-MM-dd and ``tlds`` optional). The payload
may be JSON or CSV, possibly gzip or zip compressed, so parsing is defensive
and normalises every record to ``{"domain", "tld", "drop_date", "registrar"}``.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Iterable

from ..common import http

WHOISFREAKS_DROPPED_URL = "https://files.whoisfreaks.com/v3.1/domains/dropped"
# Free public sample feed (no key): 10,000 dropped domains per day, partial gTLD coverage.
FREE_FEED_URL = "https://raw.githubusercontent.com/WhoisFreaks/daily-expired-and-dropped-domains/main/{name}"

DOMAIN_KEYS = ("domain", "domain_name", "domainName", "name", "domainname")
DROP_DATE_KEYS = ("drop_date", "dropDate", "drop_time", "date", "delete_date", "deleteDate", "dropped_date")
REGISTRAR_KEYS = ("registrar", "registrar_name", "registrarName", "domain_registrar")
LIST_KEYS = ("domains", "data", "results", "dropped_domains", "records", "items")


def fetch_dropped_domains(
    api_key: str,
    *,
    date: str | None = None,
    tlds: Iterable[str] = ("com", "ai"),
    timeout: float = 180.0,
) -> list[dict[str, Any]]:
    params = {"apiKey": api_key, "date": date, "tlds": ",".join(tlds) if tlds else None}
    try:
        resp = http.request("GET", WHOISFREAKS_DROPPED_URL, params=params, timeout=timeout)
    except http.HttpError as exc:
        raise SourceError(describe_whoisfreaks_error(exc)) from exc
    return parse_dropped_payload(resp.body)


class SourceError(Exception):
    """A feed could not be fetched; the message is written for the operator."""


def describe_whoisfreaks_error(exc: http.HttpError) -> str:
    detail = exc.body.decode("utf-8", errors="replace").strip()[:300]
    hints = {
        401: "WhoisFreaks rejected the request (401). On this endpoint that means one of: the key is wrong or inactive, or the account has no active Domainer package. The Expired/Dropped Domains feed is sold as the separate 'Domainer' package (see whoisfreaks.com/pricing), not included with WHOIS or free API plans. Check WHOISFREAKS_API_KEY has no spaces and that the Domainer package is active in the billing dashboard.",
        402: "WhoisFreaks reports no credit or an inactive plan (402). Activate the Domainer package in the WhoisFreaks billing dashboard.",
        403: "WhoisFreaks refused the request (403). The key is valid but not allowed to use the dropped-domains feed; enable the Domainer package on your plan.",
        404: "WhoisFreaks has no dropped-domains file for the requested date (404). Try --date with an earlier day; the daily file is usually published around 03:00 UTC.",
        429: "WhoisFreaks rate limit hit (429). Wait a few minutes and re-run.",
    }
    hint = hints.get(exc.status or 0, f"WhoisFreaks request failed with HTTP {exc.status}.")
    return f"{hint} Server said: {detail or '(empty response)'}"


def free_feed_filename(date: str | None) -> str:
    return f"{date}-free-dropped-domains.csv" if date else "0-latest-free-dropped-domains.csv"


def fetch_free_dropped_domains(*, date: str | None = None, timeout: float = 120.0) -> list[dict[str, Any]]:
    """Fetch the free WhoisFreaks GitHub sample feed (no API key).

    ``date`` selects that day's file (YYYY-MM-DD); if it does not exist yet the
    latest file is used instead. Coverage is a 10k/day sample, so this is a
    starting point rather than the full drop list.
    """
    url = FREE_FEED_URL.format(name=free_feed_filename(date))
    try:
        resp = http.request("GET", url, timeout=timeout)
    except http.HttpError as exc:
        if exc.status == 404 and date:
            resp = http.request("GET", FREE_FEED_URL.format(name=free_feed_filename(None)), timeout=timeout)
        else:
            raise SourceError(f"Free dropped-domains feed unavailable: {exc}") from exc
    records = parse_dropped_payload(resp.body)
    for record in records:
        record.setdefault("drop_date", date)
        record["source"] = "whoisfreaks-free"
    return records


def load_fixture(path: str | Path) -> list[dict[str, Any]]:
    return parse_dropped_payload(Path(path).read_bytes())


def decompress(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    if body[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                return b""
            return archive.read(names[0])
    return body


def parse_dropped_payload(body: bytes) -> list[dict[str, Any]]:
    raw = decompress(body).lstrip()
    if not raw:
        return []
    records: list[dict[str, Any]] = []
    if raw[:1] in (b"[", b"{"):
        data = json.loads(raw.decode("utf-8"))
        for item in _iter_json_records(data):
            normalised = normalise_record(item)
            if normalised:
                records.append(normalised)
        return records
    text = raw.decode("utf-8", errors="replace")
    for item in _iter_csv_records(text):
        normalised = normalise_record(item)
        if normalised:
            records.append(normalised)
    return records


def _iter_json_records(data: Any) -> Iterable[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in LIST_KEYS:
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def _iter_csv_records(text: str) -> Iterable[Any]:
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        return []
    first = [cell.strip() for cell in rows[0]]
    has_header = any(cell.lower() in {k.lower() for k in DOMAIN_KEYS} for cell in first)
    if has_header:
        return [dict(zip(first, row)) for row in rows[1:]]
    # Header-less feed: assume the first column is the domain.
    return [{"domain": row[0]} for row in rows]


def _first(record: dict[str, Any], keys: Iterable[str]) -> Any:
    lowered = {k.lower(): v for k, v in record.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
            return lowered[key.lower()]
    return None


def normalise_record(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        item = {"domain": item}
    if not isinstance(item, dict):
        return None
    domain = _first(item, DOMAIN_KEYS)
    if not isinstance(domain, str):
        return None
    domain = domain.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return None
    tld = domain.rsplit(".", 1)[1]
    record = {
        "domain": domain,
        "tld": tld,
        "drop_date": _first(item, DROP_DATE_KEYS),
        "registrar": _first(item, REGISTRAR_KEYS),
    }
    # Keep fixture-only metadata so dry runs can skip paid enrichment.
    if "_fixture_metrics" in item:
        record["_fixture_metrics"] = item["_fixture_metrics"]
    return record
