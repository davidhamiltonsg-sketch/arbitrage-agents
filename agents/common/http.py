"""Minimal HTTP client on urllib with retries and exponential backoff."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
DEFAULT_USER_AGENT = "arbitrage-agents/1.0 (+https://github.com/davidhamiltonsg-sketch/arbitrage-agents)"


class HttpError(Exception):
    def __init__(self, message: str, *, status: int | None = None, url: str = "", body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        detail = self.body.decode("utf-8", errors="replace").strip()
        return f"{base}: {detail[:500]}" if detail else base


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


SECRET_PARAMS = ("apikey", "api_key", "key", "token", "password")


def redact_url(url: str) -> str:
    """Mask credential-looking query parameters so URLs are safe to log."""
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in SECRET_PARAMS else v) for k, v in pairs]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(masked)))


def build_url(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    if not clean:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urllib.parse.urlencode(clean, doseq=True)}"


def request(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 30.0,
    retries: int = 3,
    backoff: float = 1.0,
    sleep=time.sleep,
) -> Response:
    """Perform an HTTP request, retrying on transient failures.

    ``retries`` is the number of attempts after the first one. Backoff is
    ``backoff * 2**attempt`` seconds unless the server sends ``Retry-After``.
    """
    full_url = build_url(url, params)
    final_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json, text/plain, */*"}
    if headers:
        final_headers.update(headers)
    payload = data
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(full_url, data=payload, headers=final_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, body, full_url)
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            last_error = HttpError(f"HTTP {exc.code} for {redact_url(full_url)}", status=exc.code, url=full_url, body=body)
            if exc.code not in RETRY_STATUSES or attempt == retries:
                raise last_error from exc
            delay = _retry_delay(exc.headers.get("Retry-After") if exc.headers else None, backoff, attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = HttpError(f"network error for {redact_url(full_url)}: {exc}", url=full_url)
            if attempt == retries:
                raise last_error from exc
            delay = backoff * (2 ** attempt)
        sleep(delay)
    assert last_error is not None
    raise last_error


MAX_RETRY_DELAY = 15.0


def _retry_delay(retry_after: str | None, backoff: float, attempt: int) -> float:
    if retry_after:
        try:
            return min(MAX_RETRY_DELAY, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(MAX_RETRY_DELAY, backoff * (2 ** attempt))


def get_json(url: str, **kwargs: Any) -> Any:
    return request("GET", url, **kwargs).json()


def post_json(url: str, json_body: Any, **kwargs: Any) -> Any:
    return request("POST", url, json_body=json_body, **kwargs).json()
