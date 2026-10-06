"""Phase 7: Slack digest with direct registrar checkout links."""
from __future__ import annotations

import urllib.parse
from typing import Any

from ..common import slack


def registrar_links(domain: str) -> dict[str, str]:
    encoded = urllib.parse.quote(domain, safe="")
    return {
        "godaddy": f"https://www.godaddy.com/domainsearch/find?domainToCheck={encoded}",
        "namecheap": f"https://www.namecheap.com/domains/registration/results/?domain={encoded}",
        "afternic": f"https://www.afternic.com/domain/{encoded}",
    }


def format_item(index: int, item: dict[str, Any]) -> str:
    links = registrar_links(item["domain"])
    return (
        f"*{index}. {slack.escape(item['domain'])}* — `Score: {item['score']}/10` — *Est. Flip: ${item['suggested_price']:,}*\n"
        f"• Metrics: *DR {item.get('dr', 0):g}* | *{item.get('referring_domains', 0)} Ref Domains* | "
        f"*{item.get('total_backlinks', 0)} Backlinks* | Brandability: *{item['brandability']}/10*\n"
        f"• Rationale: _{slack.escape(item.get('reasoning', ''))}_\n"
        f"• Checkout: <{links['godaddy']}|🛒 Register on GoDaddy> | <{links['namecheap']}|🛒 Register on Namecheap>"
    )


def build_digest(items: list[dict[str, Any]], date_str: str, *, funnel: dict[str, int] | None = None) -> tuple[str, list[dict[str, Any]]]:
    text = f"🔍 Domain Flips Daily Shortlist — {date_str}"
    blocks: list[dict[str, Any]] = [slack.header(f"🔍 High-Yield Dropped Domains ({date_str})"), slack.divider()]
    if not items:
        blocks.append(slack.section("_No domains cleared the authority gate today._"))
    for index, item in enumerate(items, start=1):
        blocks.append(slack.section(format_item(index, item)))
    if funnel:
        summary = " → ".join(f"{stage}: {count}" for stage, count in funnel.items())
        blocks.append(slack.context(f"Funnel · {summary}"))
    return text, slack.clamp_blocks(blocks)
