"""Embedding providers for semantic memory. Same pattern as LLM providers: a registry of small classes."""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

import httpx

from ..core.registry import Registry

HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests inject a mock transport


class Embedder(ABC):
    model: str

    def __init__(self, **opts: Any) -> None:
        self.opts = opts
        self.model = opts.get("model", "")

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in order."""


embedders: Registry[type[Embedder]] = Registry("embedder")


@embedders.register("openai")
class OpenAIEmbedder(Embedder):
    """OpenAI or any OpenAI-compatible ``/embeddings`` endpoint (Ollama /v1, vLLM, LM Studio...)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        base = self.opts.get("base_url", "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {self.opts['api_key']}"} if self.opts.get("api_key") else {}
        async with httpx.AsyncClient(transport=HTTP_TRANSPORT, timeout=30) as c:
            r = await c.post(f"{base}/embeddings", headers=headers, json={"model": self.model, "input": texts})
            r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


@embedders.register("ollama")
class OllamaEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        host = self.opts.get("host", "http://localhost:11434").rstrip("/")
        async with httpx.AsyncClient(transport=HTTP_TRANSPORT, timeout=60) as c:
            r = await c.post(f"{host}/api/embed", json={"model": self.model, "input": texts})
            r.raise_for_status()
        return r.json()["embeddings"]


@embedders.register("bedrock")
class BedrockEmbedder(Embedder):
    """Amazon Titan / Cohere embeddings through Bedrock (AWS credential chain)."""

    def __init__(self, **opts: Any) -> None:
        super().__init__(**opts)
        self.model = opts.get("model", "amazon.titan-embed-text-v2:0")
        self._client: Any = None

    def _one(self, text: str) -> list[float]:
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self.opts.get("region_name"))
        body = json.dumps({"inputText": text})
        resp = self._client.invoke_model(modelId=self.model, body=body)
        return json.loads(resp["body"].read())["embedding"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return list(await asyncio.gather(*[asyncio.to_thread(self._one, t) for t in texts]))
