"""Render a Slack Block Kit digest as GitHub-flavoured Markdown.

Used for delivery channels that need no credentials: the GitHub Actions job
summary and a GitHub issue created with the workflow's built-in token.
"""
from __future__ import annotations

import re
from typing import Any

LINK = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
BARE_LINK = re.compile(r"<(https?://[^>]+)>")
BOLD = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
ITALIC = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")


def mrkdwn_to_markdown(text: str) -> str:
    out = LINK.sub(r"[\2](\1)", text)
    out = BARE_LINK.sub(r"<\1>", out)
    out = BOLD.sub(r"**\1**", out)
    out = ITALIC.sub(r"*\1*", out)
    out = out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return "\n".join(line.replace("• ", "- ", 1) if line.lstrip().startswith("• ") else line for line in out.split("\n"))


def blocks_to_markdown(text: str, blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "header":
            parts.append(f"## {block['text']['text']}")
        elif kind == "section":
            parts.append(mrkdwn_to_markdown(block["text"]["text"]))
        elif kind == "context":
            parts.append("\n".join(f"_{mrkdwn_to_markdown(e.get('text', ''))}_" for e in block.get("elements", [])))
        elif kind == "divider":
            parts.append("---")
    body = "\n\n".join(p for p in parts if p)
    if not body.startswith("## "):
        body = f"## {text}\n\n{body}"
    return body + "\n"
