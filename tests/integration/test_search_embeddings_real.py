"""Real search APIs and real embedding services. Each skips unless you provide its credentials.

Search:      KIRO_IT_BRAVE_KEY | KIRO_IT_TAVILY_KEY | KIRO_IT_SEARXNG_URL
Embeddings:  KIRO_IT_EMBED_PROVIDER (openai|ollama|bedrock) + KIRO_IT_EMBED_MODEL
             [+ KIRO_IT_EMBED_API_KEY, KIRO_IT_EMBED_BASE_URL, KIRO_IT_EMBED_HOST, KIRO_IT_EMBED_REGION]
"""

import math
import os

import pytest

from kiro_crew.memory.db import Database
from kiro_crew.memory.embeddings import embedders
from kiro_crew.memory.hybrid import SQLiteHybridMemoryStore
from kiro_crew.tools.web import search_backends

from .conftest import need_env


@pytest.mark.parametrize("backend, env, opts", [
    ("brave", ("KIRO_IT_BRAVE_KEY",), lambda e: {"api_key": e["KIRO_IT_BRAVE_KEY"]}),
    ("tavily", ("KIRO_IT_TAVILY_KEY",), lambda e: {"api_key": e["KIRO_IT_TAVILY_KEY"]}),
    ("searxng", ("KIRO_IT_SEARXNG_URL",), lambda e: {"url": e["KIRO_IT_SEARXNG_URL"]}),
])
async def test_real_search_backend_returns_results(backend, env, opts):
    results = await search_backends.get(backend)(opts(need_env(*env))).search("python programming language", 3)
    assert results and all({"title", "url", "snippet"} <= r.keys() for r in results)
    assert results[0]["url"].startswith("http")


def _embedder():
    env = need_env("KIRO_IT_EMBED_PROVIDER", "KIRO_IT_EMBED_MODEL")
    opts = {"model": env["KIRO_IT_EMBED_MODEL"]}
    for var, key in (("KIRO_IT_EMBED_API_KEY", "api_key"), ("KIRO_IT_EMBED_BASE_URL", "base_url"),
                     ("KIRO_IT_EMBED_HOST", "host"), ("KIRO_IT_EMBED_REGION", "region_name")):
        if os.environ.get(var):
            opts[key] = os.environ[var]
    return embedders.get(env["KIRO_IT_EMBED_PROVIDER"])(**opts)


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def test_real_embeddings_are_semantically_sensible():
    cat, feline, car = await _embedder().embed(["cat", "feline", "automobile"])
    assert len(cat) == len(feline) == len(car) > 8
    assert _cos(cat, feline) > _cos(cat, car)  # synonyms are closer than unrelated words


async def test_hybrid_memory_with_a_real_embedder_finds_a_paraphrase():
    db = Database(":memory:")
    store = SQLiteHybridMemoryStore(db, "u", embedder=_embedder())
    await store.add("The user adores kittens and has two of them at home")
    await store.add("Quarterly budget review is scheduled for March")
    hits = await store.search("does she like felines?")  # no shared keywords with the stored fact
    assert hits and "kittens" in hits[0].content
