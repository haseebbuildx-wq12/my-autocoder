"""OpenRouter (OpenAI-compatible chat completions). Key from OPENROUTER_API_KEY.

Free-model names on OpenRouter change over time; set provider_options.openrouter.model
in .github/agent.yml to a model that is currently available to you.
"""
from __future__ import annotations

import os

from .base import ErrorKind, GenerationResult, Provider, ProviderError

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(Provider):
    name = "openrouter"
    env_vars = ("OPENROUTER_API_KEY",)

    def generate(self, prompt: str, system: str | None = None, json_mode: bool = True) -> GenerationResult:
        model = self.options.get("model", "meta-llama/llama-3.3-70b-instruct:free")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": self.options.get("temperature", 0.2),
            "max_tokens": self.options.get("max_output_tokens", self.max_output_tokens),
        }
        if json_mode and self.options.get("response_format_json", False):
            payload["response_format"] = {"type": "json_object"}
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise ProviderError(ErrorKind.MISSING_CREDENTIALS, "OPENROUTER_API_KEY is not set")
        headers = {"Authorization": f"Bearer {key}", "X-Title": "autonomous-coder"}
        _, body = self._post(ENDPOINT, headers, payload)
        data = self._parse_json(body)

        if "error" in data and not data.get("choices"):
            err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
            code = err.get("code")
            msg = str(err.get("message", ""))[:300]
            if code == 429 or "rate" in msg.lower():
                raise ProviderError(ErrorKind.RATE_LIMIT, msg, status=429)
            if code in (401, 403):
                raise ProviderError(ErrorKind.AUTH if code == 401 else ErrorKind.PERMISSION, msg, status=code)
            if code == 402:
                raise ProviderError(ErrorKind.QUOTA, msg, status=402)
            raise ProviderError(ErrorKind.OUTAGE, msg or "OpenRouter returned an error")
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(ErrorKind.OUTAGE, "OpenRouter returned no choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        if not text:
            raise ProviderError(ErrorKind.OUTAGE, "OpenRouter returned empty content")
        return GenerationResult(
            text=text, provider=self.name, model=model,
            truncated=(choices[0].get("finish_reason") == "length"), usage=data.get("usage") or {},
        )
