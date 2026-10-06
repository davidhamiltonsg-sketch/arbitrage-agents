"""Slack delivery: Block Kit helpers plus a webhook or bot-token sender."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Protocol

from . import http

MAX_SECTION_CHARS = 3000
MAX_HEADER_CHARS = 150
MAX_BLOCKS = 50
TRUNCATION_MARK = " …"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATION_MARK)] + TRUNCATION_MARK


def header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": truncate(text, MAX_HEADER_CHARS), "emoji": True}}


def section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(mrkdwn, MAX_SECTION_CHARS)}}


def context(mrkdwn: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": truncate(mrkdwn, MAX_SECTION_CHARS)}]}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def clamp_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    kept = blocks[: MAX_BLOCKS - 1]
    kept.append(context(f"_{len(blocks) - len(kept)} more blocks omitted (Slack limit)_"))
    return kept


def escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats specially."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Sender(Protocol):
    def send(self, *, text: str, blocks: list[dict[str, Any]]) -> None: ...


@dataclass
class SlackClient:
    """Posts via an incoming webhook (preferred) or chat.postMessage with a bot token."""

    webhook_url: str | None = None
    bot_token: str | None = None
    channel: str | None = None
    timeout: float = 30.0

    def send(self, *, text: str, blocks: list[dict[str, Any]]) -> None:
        payload: dict[str, Any] = {"text": text, "blocks": clamp_blocks(blocks)}
        if self.webhook_url:
            resp = http.request("POST", self.webhook_url, json_body=payload, timeout=self.timeout)
            if resp.text.strip() != "ok":
                raise http.HttpError(f"Slack webhook rejected the message: {resp.text[:200]}", status=resp.status)
            return
        if self.bot_token and self.channel:
            payload["channel"] = self.channel
            result = http.post_json(
                "https://slack.com/api/chat.postMessage",
                payload,
                headers={"Authorization": f"Bearer {self.bot_token}"},
                timeout=self.timeout,
            )
            if not result.get("ok"):
                raise http.HttpError(f"Slack API error: {result.get('error', 'unknown')}")
            return
        raise ValueError("SlackClient needs a webhook_url or a bot_token plus channel")


class StdoutSender:
    """Prints the payload instead of posting; used for dry runs and --no-deliver."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.sent: list[dict[str, Any]] = []

    def send(self, *, text: str, blocks: list[dict[str, Any]]) -> None:
        payload = {"text": text, "blocks": clamp_blocks(blocks)}
        self.sent.append(payload)
        self.stream.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
