"""Structured-output scoring through the OpenAI Chat Completions API.

Only the JSON branch of the response is accepted. Markdown fences, prose and
refusals all raise, so a malformed answer never reaches the ranking step.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from . import http

# Keywords the strict Structured Outputs mode rejects. Numeric ranges are
# enforced in code after parsing instead (see ``clamp_int``).
UNSUPPORTED_STRICT_KEYWORDS = frozenset(
    {
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "minLength", "maxLength", "pattern", "format",
        "minItems", "maxItems", "uniqueItems",
        "contentEncoding", "contentMediaType", "default", "title",
    }
)


class LLMError(Exception):
    pass


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``schema`` that satisfies OpenAI strict mode.

    Every object gets ``additionalProperties: false`` and lists all of its
    properties in ``required``; unsupported validation keywords are dropped.
    """
    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items() if k not in UNSUPPORTED_STRICT_KEYWORDS}
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties", {})
            out["properties"] = {k: walk(v) for k, v in props.items()}
            out["required"] = list(props.keys())
            out["additionalProperties"] = False
        return out

    return walk(copy.deepcopy(schema))


def clamp_int(value: Any, lo: int, hi: int, default: int | None = None) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        if default is None:
            raise LLMError(f"expected a number, got {value!r}")
        number = default
    return max(lo, min(hi, number))


@dataclass
class LLMClient:
    api_key: str
    model: str = "gpt-4o-mini"
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2
    max_tokens: int = 300
    timeout: float = 60.0

    def structured(self, *, system: str, user: str, schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": strict_schema(schema)},
            },
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            payload = http.post_json(f"{self.base_url.rstrip('/')}/chat/completions", body, headers=headers, timeout=self.timeout, retries=1)
        except http.HttpError as exc:
            raise LLMError(describe_openai_error(exc)) from exc
        return parse_structured_response(payload)


def describe_openai_error(exc: http.HttpError) -> str:
    detail = ""
    try:
        detail = json.loads(exc.body.decode("utf-8")).get("error", {}).get("message", "")
    except Exception:
        detail = exc.body.decode("utf-8", errors="replace")[:200]
    hints = {
        401: "OpenAI rejected the API key (401); check OPENAI_API_KEY",
        429: "OpenAI rate limit or no credit (429); add billing credit or lower DOMAIN_MAX_LLM_SCORE",
        402: "OpenAI billing problem (402)",
    }
    return f"{hints.get(exc.status or 0, f'OpenAI request failed (HTTP {exc.status})')}: {detail}"


def parse_structured_response(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        choice = payload["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected completion payload: {json.dumps(payload)[:400]}") from exc
    message = choice.get("message", {})
    if message.get("refusal"):
        raise LLMError(f"model refused: {message['refusal']}")
    if choice.get("finish_reason") == "length":
        raise LLMError("completion truncated by max_tokens; raise the limit")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError("empty completion content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"completion was not valid JSON: {content[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("completion JSON must be an object")
    return parsed
