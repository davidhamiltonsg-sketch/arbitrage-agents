"""Audit trail: every ingested, filtered, enriched and scored record is logged.

Rows go to a local JSONL file and, optionally, to a Google Sheets Apps Script
web-app URL (POST of a JSON array of rows) so the longitudinal dataset lives
in a sheet without needing Google OAuth here.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import http


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class AuditLog:
    def __init__(self, agent: str, path: str | Path | None, sheets_webhook_url: str | None = None, run_id: str | None = None):
        self.agent = agent
        self.run_id = run_id or new_run_id()
        self.path = Path(path) if path else None
        self.sheets_webhook_url = sheets_webhook_url
        self.rows: list[dict[str, Any]] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, stage: str, asset_id: str, **payload: Any) -> dict[str, Any]:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "agent": self.agent,
            "stage": stage,
            "asset_id": asset_id,
            **payload,
        }
        self.rows.append(row)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        return row

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row["stage"]] = out.get(row["stage"], 0) + 1
        return out

    def flush_to_sheets(self) -> bool:
        if not self.sheets_webhook_url or not self.rows:
            return False
        http.request("POST", self.sheets_webhook_url, json_body={"rows": self.rows}, timeout=60)
        return True
