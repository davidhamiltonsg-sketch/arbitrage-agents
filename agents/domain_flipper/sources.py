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
    resp = http.request("GET", WHOISFREAKS_DROPPED_URL, params=params, timeout=timeout)
    return parse_dropped_payload(resp.body)


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
