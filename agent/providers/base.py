"""Provider interface shared by every AI backend.

The core agent only ever talks to this interface:
    generate() / is_available() / classify_error() / get_usage_status()
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    # Fall back to the next provider (temporary conditions)
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    OUTAGE = "outage"
    # Never fall back: record clearly and stop so a human fixes configuration
    AUTH = "auth"
    PERMISSION = "permission"
    CONFIG = "config"
    MISSING_CREDENTIALS = "missing_credentials"
    # Our request / the model's reply was unusable; counts as a failed attempt
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


FALLBACK_KINDS = {ErrorKind.RATE_LIMIT, ErrorKind.QUOTA, ErrorKind.OUTAGE}
HALT_KINDS = {ErrorKind.AUTH, ErrorKind.PERMISSION, ErrorKind.CONFIG, ErrorKind.MISSING_CREDENTIALS}


class HttpError(Exception):
    def __init__(self, status: int, headers: dict, body: str):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body


class ProviderError(Exception):
    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status


@dataclass
class GenerationResult:
    text: str
    provider: str
    model: str = ""
    truncated: bool = False
    usage: dict = field(default_factory=dict)


def http_post_json(url: str, headers: dict, payload: dict, timeout: float) -> tuple[int, dict, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
        raise HttpError(exc.code, dict(exc.headers or {}), body) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise HttpError(0, {}, f"network error: {exc}") from None


_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def extract_retry_after(headers: dict, body: str) -> float | None:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    value = headers.get("retry-after")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            ts = float(reset)
            # OpenRouter sends epoch milliseconds, others epoch seconds
            ts = ts / 1000 if ts > 1e11 else ts
            import time
            delta = ts - time.time()
            if 0 < delta < 86400:
                return delta
        except ValueError:
            pass
    m = _RETRY_DELAY_RE.search(body or "")
    if m:
        return float(m.group(1))
    return None


def classify_http(status: int, body: str, headers: dict | None = None) -> ErrorKind:
    low = (body or "").lower()
    if status == 0:
        return ErrorKind.OUTAGE
    if status == 429:
        if any(w in low for w in ("perday", "per day", "daily", "exhausted your", "billing", "insufficient")):
            return ErrorKind.QUOTA
        return ErrorKind.RATE_LIMIT
    if status == 402:
        return ErrorKind.QUOTA
    if status == 401:
        return ErrorKind.AUTH
    if status == 403:
        if "quota" in low or "rate" in low and "limit" in low:
            return ErrorKind.QUOTA
        return ErrorKind.PERMISSION
    if status == 400:
        if "api key" in low or "api_key" in low or "unauthenticated" in low:
            return ErrorKind.AUTH
        if "model" in low and ("not found" in low or "not supported" in low or "does not exist" in low):
            return ErrorKind.CONFIG
        return ErrorKind.BAD_REQUEST
    if status == 404:
        return ErrorKind.CONFIG
    if status in (408, 409, 425, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529):
        return ErrorKind.OUTAGE
    if 500 <= status < 600:
        return ErrorKind.OUTAGE
    return ErrorKind.UNKNOWN


class Provider(ABC):
    name = "base"
    env_vars: tuple[str, ...] = ()           # credentials this provider needs

    def __init__(self, options: dict | None = None, timeout: float = 180, max_output_tokens: int = 16384):
        self.options = options or {}
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.requests_made = 0
        self.last_error: str | None = None
        self.last_headers: dict = {}

    # -- credentials -------------------------------------------------------------
    def missing_credentials(self) -> list[str]:
        return [v for v in self.env_vars if not os.environ.get(v)]

    def is_available(self) -> bool:
        """True when the provider is configured with credentials (cooldowns are the manager's job)."""
        return not self.missing_credentials()

    def credential_fingerprint(self) -> str:
        joined = "|".join(os.environ.get(v, "") for v in self.env_vars)
        return hashlib.sha256(joined.encode()).hexdigest()[:12]

    # -- API ---------------------------------------------------------------------
    @abstractmethod
    def generate(self, prompt: str, system: str | None = None, json_mode: bool = True) -> GenerationResult:
        ...

    def classify_error(self, exc: BaseException) -> ErrorKind:
        if isinstance(exc, ProviderError):
            return exc.kind
        if isinstance(exc, HttpError):
            return classify_http(exc.status, exc.body, exc.headers)
        return ErrorKind.UNKNOWN

    def get_usage_status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": self.is_available(),
            "requests_this_run": self.requests_made,
            "last_error": self.last_error,
            "rate_limit_headers": {k: v for k, v in self.last_headers.items() if "ratelimit" in k or k == "retry-after"},
        }

    # -- helper for subclasses ---------------------------------------------------
    def _post(self, url: str, headers: dict, payload: dict) -> tuple[dict, str]:
        self.requests_made += 1
        try:
            _, resp_headers, body = http_post_json(url, headers, payload, self.timeout)
        except HttpError as exc:
            self.last_error = f"HTTP {exc.status}"
            self.last_headers = exc.headers
            raise ProviderError(self.classify_error(exc), _safe_message(exc),
                                retry_after=extract_retry_after(exc.headers, exc.body),
                                status=exc.status) from None
        self.last_headers = {k.lower(): v for k, v in resp_headers.items()}
        return self.last_headers, body

    def _parse_json(self, body: str) -> dict:
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError(ErrorKind.OUTAGE, f"provider returned non-JSON body: {exc}") from None
        if not isinstance(data, dict):
            raise ProviderError(ErrorKind.OUTAGE, "provider returned an unexpected JSON shape")
        return data


def _safe_message(exc: HttpError) -> str:
    body = (exc.body or "").strip().replace("\n", " ")
    return f"HTTP {exc.status}: {body[:300]}"
