"""Phase 5: AI valuation with a strict JSON schema, plus a heuristic fallback."""
from __future__ import annotations

from typing import Any

from ..common.llm import LLMClient, clamp_int

SCHEMA_NAME = "domain_score_evaluation"

SYSTEM_PROMPT = """You are a conservative, veteran domain portfolio investor and aftermarket broker. Evaluate the commercial liquidation value of the provided expired domain.

Evaluation Criteria:
1. Brandability: 1-10 (Pronounceable, memorable, passes the radio test, free of awkward syllable combinations, commercial utility).
2. Domain Rating & Link Equity:
   - DR >= 30 AND Referring Domains >= 50: Score range 9-10
   - DR 20-29 AND Referring Domains >= 20: Score range 7-8
   - DR 10-19 AND Referring Domains >= 5: Score range 5-6
   - DR < 10: Score range 1-4
3. Resale Estimate: Provide a realistic wholesale buy-now flip price in USD (typically $300 to $3,500).

Return valid JSON adhering strictly to the required schema. No commentary outside JSON."""

USER_TEMPLATE = """Evaluate the following metrics:
Domain: {domain}
Domain Rating (DR): {dr}
Referring Domains: {referring_domains}
Total Backlinks: {total_backlinks}"""

# Ranges are documented in descriptions and enforced in code: strict mode
# rejects minimum/maximum keywords.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Composite investment grade score from 1 (worthless) to 10 (prime acquisition)."},
        "brandability": {"type": "integer", "description": "Brand suitability, memorability and commercial fit, 1 to 10."},
        "suggested_price": {"type": "integer", "description": "Estimated resale liquidation value in whole USD."},
        "reasoning": {"type": "string", "description": "Single-sentence justification synthesising backlink equity and brand phonetic appeal."},
    },
    "required": ["score", "brandability", "suggested_price", "reasoning"],
    "additionalProperties": False,
}


def normalise_scores(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": clamp_int(raw.get("score"), 1, 10),
        "brandability": clamp_int(raw.get("brandability"), 1, 10),
        "suggested_price": clamp_int(raw.get("suggested_price"), 0, 1_000_000),
        "reasoning": str(raw.get("reasoning") or "").strip()[:500],
    }


def score_domain(record: dict[str, Any], llm: LLMClient) -> dict[str, Any]:
    def show(value: Any) -> Any:
        return "unknown" if value is None else value

    user = USER_TEMPLATE.format(
        domain=record["domain"],
        dr=show(record.get("dr")),
        referring_domains=show(record.get("referring_domains")),
        total_backlinks=show(record.get("total_backlinks")),
    )
    raw = llm.structured(system=SYSTEM_PROMPT, user=user, schema_name=SCHEMA_NAME, schema=SCHEMA)
    return normalise_scores(raw)


def heuristic_score(record: dict[str, Any]) -> dict[str, Any]:
    """Rubric-only scorer used in dry runs and when no OpenAI key is configured."""
    dr_raw = record.get("dr")
    refs_raw = record.get("referring_domains")
    dr = float(dr_raw or 0)
    refs = int(refs_raw or 0)
    sld = record["domain"].split(".")[0]

    if refs_raw is None:  # authority score only (e.g. Open PageRank), no link counts
        equity = 9 if dr >= 30 else 7 if dr >= 20 else 5 if dr >= 10 else 2
    elif dr >= 30 and refs >= 50:
        equity = 9
    elif dr >= 20 and refs >= 20:
        equity = 7
    elif dr >= 10 and refs >= 5:
        equity = 5
    else:
        equity = 2
    if dr_raw is None:  # no authority data at all: rank on the name alone
        equity = 3

    vowels = sum(ch in "aeiou" for ch in sld)
    ratio = vowels / max(len(sld), 1)
    brandability = 5
    if 0.3 <= ratio <= 0.6:
        brandability += 2
    if len(sld) <= 8:
        brandability += 2
    elif len(sld) <= 12:
        brandability += 1
    if record.get("tld") == "ai":
        brandability += 1
    brandability = max(1, min(10, brandability))

    score = max(1, min(10, round(0.7 * equity + 0.3 * brandability)))
    price = int(300 + (score - 1) * 350 + min(refs, 200) * 2)
    if dr_raw is None:
        authority = "no authority data"
    elif refs_raw is None:
        authority = f"authority score {dr:g}/100 (no link counts)"
    else:
        authority = f"DR {dr:g} with {refs} referring domains"
    return {
        "score": score,
        "brandability": brandability,
        "suggested_price": price,
        "reasoning": f"Heuristic: {authority}, {len(sld)}-letter {record.get('tld', '')} name.",
    }
