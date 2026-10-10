"""Provider selection, fallback and persistent cooldown tracking.

Rules (from the spec):
* fall back automatically ONLY for rate limits, exhausted quotas and temporary outages;
* never fall back on invalid key / missing credentials / config / permission errors:
  record them clearly and halt so a human can fix them;
* provider state survives the process (it lives in PROGRESS.json) so a later run knows
  who is cooling down and until when.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..util import now_iso, parse_iso
from .base import ErrorKind, FALLBACK_KINDS, GenerationResult, HALT_KINDS, Provider, ProviderError


class NoProviderAvailable(Exception):
    def __init__(self, message: str, next_available_at: str | None = None):
        super().__init__(message)
        self.next_available_at = next_available_at


class ConfigBlocked(Exception):
    def __init__(self, provider: str, kind: ErrorKind, message: str):
        super().__init__(f"{provider}: {kind.value}: {message}")
        self.provider = provider
        self.kind = kind
        self.detail = message


class ProviderManager:
    def __init__(self, providers: list[Provider], state, cfg: dict, log: logging.Logger,
                 on_unavailable: Callable[[str, ErrorKind], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.providers = providers
        self.state = state
        self.cfg = cfg
        self.log = log
        self.on_unavailable = on_unavailable
        self.clock = clock

    # -- state helpers -----------------------------------------------------------
    def _pstate(self, name: str) -> dict:
        return self.state.progress.setdefault("providers", {}).setdefault(name, {})

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=timezone.utc)

    def _cooling(self, name: str) -> datetime | None:
        until = self._pstate(name).get("unavailable_until")
        if not until:
            return None
        dt = parse_iso(until)
        return dt if dt > self._now() else None

    # -- blocks (config errors) --------------------------------------------------
    def assert_not_blocked(self) -> None:
        """Raise ConfigBlocked if a configured provider is blocked by an unfixed config error.
        A block lifts automatically when the credential changes (fingerprint differs)."""
        for p in self.providers:
            st = self._pstate(p.name)
            block = st.get("blocked")
            if not block:
                continue
            if block.get("fingerprint") != p.credential_fingerprint():
                self.log.info("credentials for %s changed; clearing config block", p.name)
                st.pop("blocked", None)
                continue
            raise ConfigBlocked(p.name, ErrorKind(block["kind"]), block.get("message", ""))

    def clear_blocks(self) -> None:
        for p in self.providers:
            self._pstate(p.name).pop("blocked", None)

    # -- core --------------------------------------------------------------------
    def usable(self) -> list[Provider]:
        return [p for p in self.providers if p.is_available()]

    def generate(self, prompt: str, system: str | None = None, avoid: list[str] | tuple[str, ...] = ()) -> GenerationResult:
        self.assert_not_blocked()
        usable = self.usable()
        if not usable:
            missing = {p.name: p.missing_credentials() for p in self.providers}
            raise ConfigBlocked(self.providers[0].name if self.providers else "none",
                                ErrorKind.MISSING_CREDENTIALS,
                                f"no configured provider has credentials: {missing}")
        preferred = [p for p in usable if p.name not in avoid]
        order = preferred + [p for p in usable if p.name in avoid]
        soonest: datetime | None = None
        errors: dict[str, str] = {}
        for p in order:
            cool = self._cooling(p.name)
            if cool is not None:
                soonest = cool if soonest is None else min(soonest, cool)
                errors[p.name] = f"cooling down until {cool.isoformat()}"
                continue
            try:
                result = p.generate(prompt, system=system, json_mode=True)
            except Exception as exc:  # noqa: BLE001 - classification decides what to do
                kind = p.classify_error(exc)
                if kind == ErrorKind.UNKNOWN:
                    kind = ErrorKind.OUTAGE
                message = str(exc)[:300]
                if kind in FALLBACK_KINDS:
                    until = self._mark_unavailable(p, kind, getattr(exc, "retry_after", None), message)
                    soonest = until if soonest is None else min(soonest, until)
                    errors[p.name] = f"{kind.value}: {message}"
                    continue
                if kind in HALT_KINDS:
                    self._block(p, kind, message)
                    raise ConfigBlocked(p.name, kind, message) from None
                raise  # BAD_REQUEST etc.: the caller treats it as a failed attempt
            st = self._pstate(p.name)
            st["consecutive_failures"] = 0
            st["last_success_at"] = now_iso()
            st.pop("unavailable_until", None)
            self.state.progress["last_provider"] = p.name
            return result
        raise NoProviderAvailable(
            "no AI provider is currently available: " + "; ".join(f"{k}: {v}" for k, v in errors.items()),
            soonest.isoformat() if soonest else None,
        )

    # -- bookkeeping -------------------------------------------------------------
    def _mark_unavailable(self, p: Provider, kind: ErrorKind, retry_after: float | None, message: str) -> datetime:
        st = self._pstate(p.name)
        failures = int(st.get("consecutive_failures", 0)) + 1
        base = {ErrorKind.RATE_LIMIT: self.cfg["cooldowns"]["rate_limit"],
                ErrorKind.QUOTA: self.cfg["cooldowns"]["quota"],
                ErrorKind.OUTAGE: self.cfg["cooldowns"]["outage"]}[kind]
        seconds = base * (2 ** (failures - 1))
        if retry_after:
            seconds = max(seconds if failures > 1 else 0, retry_after + 5)
        seconds = min(seconds, self.cfg["max_cooldown_seconds"])
        until = self._now() + timedelta(seconds=seconds)
        st.update({
            "consecutive_failures": failures,
            "unavailable_until": until.replace(microsecond=0).isoformat(),
            "last_error_kind": kind.value,
            "last_error": message,
            "last_error_at": now_iso(),
        })
        self.log.warning("provider %s unavailable (%s) until %s", p.name, kind.value, st["unavailable_until"])
        self.state.log_decision(f"Provider {p.name} marked unavailable ({kind.value}) until {st['unavailable_until']}")
        if self.on_unavailable:
            self.on_unavailable(p.name, kind)   # save state + checkpoint BEFORE trying the next provider
        return until

    def _block(self, p: Provider, kind: ErrorKind, message: str) -> None:
        st = self._pstate(p.name)
        st["blocked"] = {"kind": kind.value, "message": message, "at": now_iso(),
                         "fingerprint": p.credential_fingerprint()}
        self.state.log_decision(
            f"Provider {p.name} reported {kind.value}: {message}. Not falling back automatically; "
            f"fix the configuration/credentials and re-run.")

    def status(self) -> list[dict]:
        out = []
        for p in self.providers:
            st = self._pstate(p.name)
            out.append({**p.get_usage_status(),
                        "missing_credentials": p.missing_credentials(),
                        "unavailable_until": st.get("unavailable_until"),
                        "blocked": st.get("blocked")})
        return out
