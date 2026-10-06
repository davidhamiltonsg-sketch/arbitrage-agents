"""Phase 3: zero-cost deterministic filters applied before any paid call."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_TRADEMARK_TERMS: tuple[str, ...] = (
    "apple", "google", "meta", "facebook", "amazon", "microsoft", "nike", "netflix",
    "openai", "anthropic", "stripe", "paypal", "tesla", "instagram", "youtube",
    "whatsapp", "twitter", "tiktok", "samsung", "adobe", "disney", "walmart",
)


@dataclass
class FilterConfig:
    max_sld_length: int = 20
    allowed_tlds: tuple[str, ...] = ("com", "ai")
    trademark_terms: tuple[str, ...] = DEFAULT_TRADEMARK_TERMS
    allow_hyphens: bool = False
    allow_digits: bool = False
    extra_blocklist: tuple[str, ...] = field(default_factory=tuple)


def split_domain(domain: str) -> tuple[str, str]:
    """Return (second-level label, tld). ``foo.co.uk`` -> (``foo``, ``co.uk``)."""
    parts = domain.lower().strip().split(".")
    if len(parts) < 2:
        return domain.lower(), ""
    return parts[0], ".".join(parts[1:])


def check_domain(domain: str, cfg: FilterConfig) -> str | None:
    """Return a rejection reason, or None when the domain passes every rule."""
    sld, tld = split_domain(domain)
    if not sld or not tld:
        return "malformed"
    if tld not in cfg.allowed_tlds:
        return f"tld:{tld}"
    if sld.startswith("xn--"):
        return "idn"
    if len(sld) > cfg.max_sld_length:
        return f"length:{len(sld)}"
    if len(sld) < 2:
        return "too-short"
    if not cfg.allow_hyphens and "-" in sld:
        return "hyphen"
    if not cfg.allow_digits and any(ch.isdigit() for ch in sld):
        return "digit"
    if not sld.isascii() or not sld.replace("-", "").isalnum():
        return "non-ascii"
    for term in cfg.trademark_terms:
        if sld.startswith(term):
            return f"trademark:{term}"
    for term in cfg.extra_blocklist:
        if term and term in sld:
            return f"blocklist:{term}"
    return None


def apply_filters(
    records: Iterable[dict[str, Any]], cfg: FilterConfig
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    kept: list[dict[str, Any]] = []
    rejected: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for record in records:
        domain = record["domain"]
        if domain in seen:
            rejected.append((record, "duplicate"))
            continue
        seen.add(domain)
        reason = check_domain(domain, cfg)
        if reason:
            rejected.append((record, reason))
        else:
            kept.append(record)
    return kept, rejected
