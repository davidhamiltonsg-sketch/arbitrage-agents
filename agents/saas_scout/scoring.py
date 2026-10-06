"""Phase 5: modernisation scoring with a strict schema, plus a heuristic fallback."""
from __future__ import annotations

from typing import Any

from ..common.llm import LLMClient, clamp_int

SCHEMA_NAME = "saas_rebuild_evaluation"

SYSTEM_PROMPT = """You are an expert micro-private equity investor and full-stack AI engineer. You specialize in buying obsolete, abandoned web tools and converting their rigid dashboards into autonomous, agent-native workflows.

Evaluation Objectives:
1. Feasibility of Agent-Native Rebuild: Can this tool's manual user workflows (e.g. clicking buttons, copy-pasting text, running manual reports) be completely replaced by an LLM-driven agent (e.g. LangGraph, FastAPI, a Chrome extension calling a hosted model API) in under 16 hours of engineering?
2. Score: 1-10 overall investment grade.
3. Estimated Flip Value: Fair market value once refactored with a modern agent UI and active subscription billing.
4. One-Sentence Rebuild Blueprint: Concrete architecture statement (e.g. "Replace brittle Selenium scraper with crawl4ai + structured LLM parsing").

Return strictly valid JSON matching the required schema."""

USER_TEMPLATE = """Analyze this neglected asset:
Product Name: {name}
Category: {category}
Current Tech Stack: {tech_stack}
Monthly Traffic / Installs: {traffic_or_installs}
Last Updated: {last_updated_date}
Asking Price: {asking_price}
Product Description: {description}"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Composite acquisition viability score from 1 to 10."},
        "agent_rewrite_potential": {"type": "integer", "description": "How readily manual workflows convert into autonomous LLM functions, 1 to 10."},
        "estimated_flip_value": {"type": "integer", "description": "Projected resale valuation in USD following agent modernisation."},
        "rebuild_architecture_blueprint": {"type": "string", "description": "Precise technical stack prescription to build the replacement agent in one weekend."},
        "reasoning": {"type": "string", "description": "One-sentence investment summary weighing user retention against execution complexity."},
    },
    "required": ["score", "agent_rewrite_potential", "estimated_flip_value", "rebuild_architecture_blueprint", "reasoning"],
    "additionalProperties": False,
}


def normalise_scores(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": clamp_int(raw.get("score"), 1, 10),
        "agent_rewrite_potential": clamp_int(raw.get("agent_rewrite_potential"), 1, 10),
        "estimated_flip_value": clamp_int(raw.get("estimated_flip_value"), 0, 10_000_000),
        "rebuild_architecture_blueprint": str(raw.get("rebuild_architecture_blueprint") or "").strip()[:600],
        "reasoning": str(raw.get("reasoning") or "").strip()[:500],
    }


def traffic_label(listing: dict[str, Any]) -> str:
    parts = []
    if listing.get("monthly_visits"):
        parts.append(f"{listing['monthly_visits']:,} monthly visits")
    if listing.get("claimed_users"):
        parts.append(f"{listing['claimed_users']:,} claimed users/installs")
    return " / ".join(parts) or "unknown"


def score_listing(listing: dict[str, Any], llm: LLMClient) -> dict[str, Any]:
    price = listing.get("asking_price")
    user = USER_TEMPLATE.format(
        name=listing.get("name"),
        category=listing.get("category"),
        tech_stack=", ".join(listing.get("tech_stack") or []) or "unknown",
        traffic_or_installs=traffic_label(listing),
        last_updated_date=listing.get("last_updated_date") or "unknown",
        asking_price=f"${price:,.0f}" if price is not None else "not listed (outreach target)",
        description=(listing.get("description") or "n/a")[:1500],
    )
    raw = llm.structured(system=SYSTEM_PROMPT, user=user, schema_name=SCHEMA_NAME, schema=SCHEMA)
    return normalise_scores(raw)


def heuristic_score(listing: dict[str, Any]) -> dict[str, Any]:
    users = int(listing.get("claimed_users") or 0)
    visits = int(listing.get("monthly_visits") or 0)
    price = float(listing.get("asking_price") or 0)
    stack = " ".join(listing.get("tech_stack") or []).lower()

    footprint = min(5, (users // 2000) + (visits // 2000))
    legacy = 3 if any(term in stack for term in ("jquery", "php", "vue 2", "mv2", "vanilla", "rails", "flask")) else 1
    affordability = 2 if price and price <= 5000 else 1
    score = max(1, min(10, footprint + legacy + affordability))
    potential = max(1, min(10, 6 + legacy))
    flip = int(max(price * 3, (users + visits) * 1.5, 2000))
    return {
        "score": score,
        "agent_rewrite_potential": potential,
        "estimated_flip_value": flip,
        "rebuild_architecture_blueprint": "Heuristic: wrap the existing data model in a FastAPI service and replace the manual dashboard with a single LLM-driven workflow agent.",
        "reasoning": f"Heuristic: {users:,} users, {visits:,} visits, legacy stack signal {legacy}/3, asking ${price:,.0f}.",
    }
