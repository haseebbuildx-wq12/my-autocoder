"""Provider registry. Add a new backend by subclassing `Provider` and registering it here."""
from __future__ import annotations

from .base import ErrorKind, GenerationResult, Provider, ProviderError
from .cloudflare import CloudflareProvider
from .gemini import GeminiProvider
from .manager import ConfigBlocked, NoProviderAvailable, ProviderManager
from .openrouter import OpenRouterProvider

REGISTRY: dict[str, type[Provider]] = {
    "gemini": GeminiProvider,
    "openrouter": OpenRouterProvider,
    "cloudflare": CloudflareProvider,
}


def build_providers(cfg: dict) -> list[Provider]:
    """Instantiate configured providers in priority order. Providers without credentials are
    kept in the list but report is_available() == False, so they are simply skipped."""
    providers: list[Provider] = []
    for name in cfg["providers"]:
        cls = REGISTRY[name]
        providers.append(cls(cfg["provider_options"].get(name, {}),
                             timeout=cfg["request_timeout_seconds"],
                             max_output_tokens=cfg["max_output_tokens"]))
    return providers


__all__ = [
    "ErrorKind", "GenerationResult", "Provider", "ProviderError", "ProviderManager",
    "NoProviderAvailable", "ConfigBlocked", "REGISTRY", "build_providers",
]
