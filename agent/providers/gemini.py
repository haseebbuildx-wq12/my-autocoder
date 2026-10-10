"""Google Gemini (generativelanguage REST API). Free tier friendly; key from GEMINI_API_KEY."""
from __future__ import annotations

import os

from .base import ErrorKind, GenerationResult, Provider, ProviderError

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(Provider):
    name = "gemini"
    env_vars = ("GEMINI_API_KEY",)

    def generate(self, prompt: str, system: str | None = None, json_mode: bool = True) -> GenerationResult:
        model = self.options.get("model", "gemini-2.5-flash")
        gen_cfg = {
            "temperature": self.options.get("temperature", 0.2),
            "maxOutputTokens": self.options.get("max_output_tokens", self.max_output_tokens),
        }
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"
        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_cfg,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise ProviderError(ErrorKind.MISSING_CREDENTIALS, "GEMINI_API_KEY is not set")
        # The key travels in a header, never in the URL, so it cannot leak into logs.
        _, body = self._post(ENDPOINT.format(model=model), {"x-goog-api-key": key}, payload)
        data = self._parse_json(body)

        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ProviderError(ErrorKind.BAD_REQUEST, f"prompt blocked by Gemini: {feedback['blockReason']}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError(ErrorKind.OUTAGE, "Gemini returned no candidates")
        cand = candidates[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        finish = cand.get("finishReason", "")
        if not text and finish not in ("MAX_TOKENS",):
            raise ProviderError(ErrorKind.BAD_REQUEST, f"Gemini returned empty text (finishReason={finish})")
        usage = data.get("usageMetadata") or {}
        return GenerationResult(text=text, provider=self.name, model=model,
                                truncated=(finish == "MAX_TOKENS"), usage=usage)
