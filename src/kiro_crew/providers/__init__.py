"""LLM providers. A provider is just ``(ModelProfile) -> strands Model``.

Add one by registering a builder; nothing else in the codebase changes::

    from kiro_crew.providers import providers

    @providers.register("my-llm")
    def build(profile): return MyModel(model_id=profile.model_id, **profile.params)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands.models import ModelRouter
from strands.models.model import Model

from ..core.config import ModelProfile, ModelsConfig
from ..core.registry import Registry

ProviderBuilder = Callable[[ModelProfile], Model]
providers: Registry[ProviderBuilder] = Registry("provider")


@providers.register("bedrock")
def _bedrock(p: ModelProfile) -> Model:
    from strands.models import BedrockModel

    return BedrockModel(model_id=p.model_id, **p.client, **p.params)  # client: region_name, boto_session...


@providers.register("anthropic")
def _anthropic(p: ModelProfile) -> Model:
    from strands.models.anthropic import AnthropicModel

    params = dict(p.params)
    max_tokens = params.pop("max_tokens", 4096)  # required by the Anthropic API
    return AnthropicModel(client_args=p.client or None, model_id=p.model_id, max_tokens=max_tokens, params=params)


@providers.register("openai")
def _openai(p: ModelProfile) -> Model:
    # Also serves any OpenAI-compatible endpoint (OpenRouter, vLLM, LM Studio, Groq, Together, ...)
    # via client: {base_url: ..., api_key: ...}.
    from strands.models.openai import OpenAIModel

    return OpenAIModel(client_args=p.client or None, model_id=p.model_id, params=p.params)


@providers.register("ollama")
def _ollama(p: ModelProfile) -> Model:
    from strands.models.ollama import OllamaModel

    host = p.client.get("host", "http://localhost:11434")
    return OllamaModel(host, model_id=p.model_id, **p.params)


@providers.register("litellm")
def _litellm(p: ModelProfile) -> Model:
    # 100+ providers behind one id scheme, e.g. "gemini/gemini-2.0-flash", "mistral/mistral-large".
    from strands.models.litellm import LiteLLMModel

    return LiteLLMModel(client_args=p.client or None, model_id=p.model_id, params=p.params)


class ModelFactory:
    """Builds and caches models from named profiles; wraps the default in a failover router."""

    def __init__(self, cfg: ModelsConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, Model] = {}

    @property
    def profile_names(self) -> list[str]:
        return list(self.cfg.profiles)

    def profile(self, name: str) -> Model:
        if name not in self._cache:
            if name not in self.cfg.profiles:
                raise KeyError(f"Unknown model profile {name!r}. Available: {self.profile_names}")
            p = self.cfg.profiles[name]
            self._cache[name] = providers.get(p.provider)(p)
        return self._cache[name]

    def resolve(self, name: str | None = None) -> Any:
        """Return a Model, or a ModelRouter (failover) when resolving the default with fallbacks."""
        name = name or self.cfg.default
        if name == self.cfg.default and self.cfg.fallback:
            chain = [name, *[n for n in self.cfg.fallback if n != name]]
            return ModelRouter([self.profile(n) for n in chain])
        return self.profile(name)
