"""Phase 7: Slack digest with direct seller contact links."""
from __future__ import annotations

from typing import Any

from ..common import slack


def format_item(index: int, item: dict[str, Any]) -> str:
    price = item.get("asking_price")
    price_text = f"${price:,.0f}" if price is not None else "not listed"
    stack = ", ".join(item.get("tech_stack") or []) or "unknown"
    visits = f"{item['monthly_visits']:,} visits/mo" if item.get("monthly_visits") else "traffic n/a"
    users = f"{item['claimed_users']:,} users" if item.get("claimed_users") else "users n/a"
    return (
        f"*{index}. {slack.escape(item['name'])}* ({slack.escape(item.get('category', ''))}) — `Score: {item['score']}/10` | *Price: {price_text}*\n"
        f"• *Agent Potential*: `{item['agent_rewrite_potential']}/10` | *Est. Flip*: `${item['estimated_flip_value']:,}`\n"
        f"• *Current Footprint*: {users} | {visits} | Last updated: {slack.escape(item.get('last_updated_date') or 'unknown')}\n"
        f"• *Stack*: {slack.escape(stack)}\n"
        f"• *Rebuild Plan*: `{slack.escape(item.get('rebuild_architecture_blueprint', ''))}`\n"
        f"• *Rationale*: {slack.escape(item.get('reasoning', ''))}\n"
        f"• *Action*: <{item.get('seller_contact_url') or item.get('source_url')}|📩 Open Deal Room / Message Founder>"
    )


def build_digest(items: list[dict[str, Any]], date_str: str, *, funnel: dict[str, int] | None = None) -> tuple[str, list[dict[str, Any]]]:
    text = f"🛠️ Dead SaaS Scout — Weekly Modernization Targets ({date_str})"
    blocks: list[dict[str, Any]] = [slack.header(f"🛠️ Dead SaaS Scout: Top {len(items)} Acquisition Opportunities")]
    if not items:
        blocks.append(slack.section("_No listings cleared the score and rewrite-potential thresholds this week._"))
    for index, item in enumerate(items, start=1):
        if index > 1:
            blocks.append(slack.divider())
        blocks.append(slack.section(format_item(index, item)))
    if funnel:
        summary = " → ".join(f"{stage}: {count}" for stage, count in funnel.items())
        blocks.append(slack.context(f"Funnel · {summary}"))
    return text, slack.clamp_blocks(blocks)
