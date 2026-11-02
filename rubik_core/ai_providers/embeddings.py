"""Workspace-bound embedding calls for versioned local vector indexes.

An index spec, rather than the AI Cost feature, chooses the embedding model.
The provider connection and key are revalidated on every call so a revoked key
cannot silently send the same document to Cubie's managed Vertex account.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests
from google import genai
from google.genai import types

from rubik_core.ai_providers.registry import (
    IncompatibleModelRoute,
    ModelRoute,
    provider_connection_route,
)
from rubik_core.cost.usage import feature_context, record_usage
from rubik_core.tenancy.context import current_workspace
from rubik_core.tenancy.secrets import workspace_secret

if TYPE_CHECKING:
    from rubik_core.ai_providers.embedding_index import EmbeddingIndexSpec


EMBEDDING_DIMENSIONS: dict[tuple[str, str], int] = {
    ("openai", "text-embedding-3-small"): 1536,
    ("gemini", "gemini-embedding-2"): 768,
    ("vertex_managed", "text-embedding-004"): 768,
}


class EmbeddingProviderError(RuntimeError):
    """A selected embedding provider failed without revealing its response."""


def embedding_dimension(provider: str, model: str) -> int:
    try:
        return EMBEDDING_DIMENSIONS[(provider, model)]
    except KeyError:
        raise IncompatibleModelRoute("Embedding model is not supported") from None


def _vectors(
    values: object, *, expected_count: int, dimension: int
) -> list[list[float]]:
    if not isinstance(values, list) or len(values) != expected_count:
        raise EmbeddingProviderError("Embedding response count was invalid")
    output: list[list[float]] = []
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) != dimension:
            raise EmbeddingProviderError("Embedding response dimension was invalid")
        vector: list[float] = []
        for value in item:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EmbeddingProviderError("Embedding response values were invalid")
            value = float(value)
            if not math.isfinite(value):
                raise EmbeddingProviderError("Embedding response values were invalid")
            vector.append(value)
        output.append(vector)
    return output


def _record(route: ModelRoute, prompt_tokens: int) -> None:
    record_usage(
        operation="embed",
        model=route.model,
        prompt_tokens=prompt_tokens,
        completion_tokens=0,
        total_tokens=prompt_tokens,
        metadata={"provider": route.provider, "billing_owner": route.billing_owner},
    )


def _embed_openai(
    route: ModelRoute, texts: list[str], dimension: int
) -> list[list[float]]:
    key = workspace_secret("ai_openai", "api_key")
    if not key:
        raise IncompatibleModelRoute("openai API key is unavailable")
    try:
        response = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": route.model,
                "input": texts,
                "dimensions": dimension,
                "encoding_format": "float",
            },
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise EmbeddingProviderError("Embedding provider request failed") from None
    if response.status_code != 200:
        raise EmbeddingProviderError(
            f"Embedding provider returned HTTP {response.status_code}"
        )
    try:
        data = response.json()
    except ValueError:
        raise EmbeddingProviderError(
            "Embedding provider response was invalid"
        ) from None
    if not isinstance(data, dict) or data.get("model") != route.model:
        raise EmbeddingProviderError("Embedding provider response was invalid")
    entries = data.get("data")
    if not isinstance(entries, list) or len(entries) != len(texts):
        raise EmbeddingProviderError("Embedding response count was invalid")
    try:
        by_index = {entry["index"]: entry["embedding"] for entry in entries}
        if set(by_index) != set(range(len(texts))):
            raise ValueError
        values = [by_index[i] for i in range(len(texts))]
    except (KeyError, TypeError, ValueError):
        raise EmbeddingProviderError(
            "Embedding provider response was invalid"
        ) from None
    vectors = _vectors(values, expected_count=len(texts), dimension=dimension)
    usage = data.get("usage") or {}
    prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if isinstance(prompt, bool) or not isinstance(prompt, int) or prompt < 0:
        prompt = sum(max(1, len(text) // 4) for text in texts)
    _record(route, prompt)
    return vectors


def _embed_gemini(
    route: ModelRoute, texts: list[str], dimension: int
) -> list[list[float]]:
    key = workspace_secret("ai_gemini", "api_key")
    if not key:
        raise IncompatibleModelRoute("gemini API key is unavailable")
    try:
        with genai.Client(
            vertexai=False,
            api_key=key,
            http_options=types.HttpOptions(timeout=30_000),
        ) as client:
            response = client.models.embed_content(
                model=route.model,
                contents=texts,
                config=types.EmbedContentConfig(output_dimensionality=dimension),
            )
        values = [item.values for item in response.embeddings or []]
    except Exception:
        raise EmbeddingProviderError("Embedding provider request failed") from None
    vectors = _vectors(values, expected_count=len(texts), dimension=dimension)
    metadata = getattr(response, "metadata", None)
    characters = getattr(metadata, "billable_character_count", None)
    prompt = (
        max(1, characters // 4)
        if isinstance(characters, int) and characters > 0
        else sum(max(1, len(text) // 4) for text in texts)
    )
    _record(route, prompt)
    return vectors


def embed_with_route(
    route: ModelRoute, texts: list[str], dimension: int
) -> list[list[float]]:
    """Embed through one allowlisted provider with no managed fallback."""
    if not texts:
        return []
    if route.workspace != current_workspace():
        raise PermissionError("Embedding route belongs to another workspace")
    if dimension != embedding_dimension(route.provider, route.model):
        raise IncompatibleModelRoute("Embedding index dimension is incompatible")
    # Revalidate connection and secret on each call; the passed route only pins
    # the intended provider/model, never authorizes stale credentials.
    route = provider_connection_route(
        route.provider, route.model, feature="embedding_index", capability="embedding"
    )
    if route.provider == "openai":
        return _embed_openai(route, texts, dimension)
    if route.provider == "gemini":
        return _embed_gemini(route, texts, dimension)
    if route.provider == "vertex_managed":
        from rubik_core.models import GenAIEmbeddings

        try:
            vectors = GenAIEmbeddings(route.model).embed_documents(texts)
        except Exception:
            raise EmbeddingProviderError("Embedding provider request failed") from None
        return _vectors(vectors, expected_count=len(texts), dimension=dimension)
    raise IncompatibleModelRoute("Embedding provider is not supported")


@dataclass
class WorkspaceEmbeddings:
    """LangChain-shaped embeddings pinned to one active index specification."""

    spec: EmbeddingIndexSpec

    def _embed(self, texts: list[str]) -> list[IndexVector]:
        from rubik_core.ai_providers.embedding_index import (
            active_embedding_index,
            embedding_index_lock,
        )

        if current_workspace() != self.spec.workspace:
            raise PermissionError("Embedding model belongs to another workspace")
        with embedding_index_lock(shared=True):
            if active_embedding_index(self.spec.corpus) != self.spec:
                raise IncompatibleModelRoute(
                    "Embedding index changed; retry this request"
                )
            route = provider_connection_route(
                self.spec.provider,
                self.spec.model,
                feature="embedding_index",
                capability="embedding",
            )
            vectors = embed_with_route(route, texts, self.spec.dimension)
            if active_embedding_index(self.spec.corpus) != self.spec:
                raise IncompatibleModelRoute(
                    "Embedding index changed; retry this request"
                )
            return [IndexVector(vector, self.spec) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)


class IndexVector(list):
    """A query vector that carries the exact index space used to create it."""

    def __init__(self, values: list[float], spec: EmbeddingIndexSpec):
        super().__init__(values)
        self.index_spec = spec


def embed_connection_probe(provider: str, model: str) -> dict[str, object]:
    """Exercise the real embedding capability before offering index rebuild."""
    dimension = embedding_dimension(provider, model)
    route = provider_connection_route(
        provider, model, feature="embedding_index", capability="embedding"
    )
    with feature_context("ai_provider_connection_test"):
        vectors = embed_with_route(
            route, ["Cubie embedding connection test"], dimension
        )
    return {"provider": provider, "model": model, "dimension": len(vectors[0])}
