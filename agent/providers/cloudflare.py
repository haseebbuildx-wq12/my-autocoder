"""Cloudflare Workers AI (REST). Needs CLOUDFLARE_API_TOKEN and an account id
(CLOUDFLARE_ACCOUNT_ID or provider_options.cloudflare.account_id)."""
from __future__ import annotations

import os

from .base import ErrorKind, GenerationResult, Provider, ProviderError

ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"


class CloudflareProvider(Provider):
    name = "cloudflare"
    env_vars = ("CLOUDFLARE_API_TOKEN",)

    def _account(self) -> str:
        return os.environ.get("CLOUDFLARE_ACCOUNT_ID") or str(self.options.get("account_id") or "")

    def missing_credentials(self) -> list[str]:
        missing = super().missing_credentials()
        if not self._account():
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        return missing

    def credential_fingerprint(self) -> str:
        import hashlib
        joined = os.environ.get("CLOUDFLARE_API_TOKEN", "") + "|" + self._account()
        return hashlib.sha256(joined.encode()).hexdigest()[:12]

    def generate(self, prompt: str, system: str | None = None, json_mode: bool = True) -> GenerationResult:
        model = self.options.get("model", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
        account = self._account()
        if not token or not account:
            raise ProviderError(ErrorKind.MISSING_CREDENTIALS, "Cloudflare token or account id missing")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "messages": messages,
            "max_tokens": self.options.get("max_output_tokens", self.max_output_tokens),
            "temperature": self.options.get("temperature", 0.2),
        }
        _, body = self._post(ENDPOINT.format(account=account, model=model),
                             {"Authorization": f"Bearer {token}"}, payload)
        data = self._parse_json(body)
        if not data.get("success", True):
            errors = data.get("errors") or []
            msg = "; ".join(str(e.get("message", e)) for e in errors)[:300]
            codes = {e.get("code") for e in errors if isinstance(e, dict)}
            low = msg.lower()
            if "rate" in low or "limit" in low or 3040 in codes:
                kind = ErrorKind.QUOTA if "daily" in low or "neurons" in low else ErrorKind.RATE_LIMIT
                raise ProviderError(kind, msg)
            if "auth" in low or "token" in low or 10000 in codes:
                raise ProviderError(ErrorKind.AUTH, msg)
            raise ProviderError(ErrorKind.OUTAGE, msg or "Cloudflare returned an error")
        result = data.get("result") or {}
        text = result.get("response") if isinstance(result, dict) else None
        if isinstance(text, (dict, list)):
            import json
            text = json.dumps(text)
        if not text:
            raise ProviderError(ErrorKind.OUTAGE, "Cloudflare returned an empty response")
        return GenerationResult(text=str(text), provider=self.name, model=model,
                                usage=(result.get("usage") or {}) if isinstance(result, dict) else {})
