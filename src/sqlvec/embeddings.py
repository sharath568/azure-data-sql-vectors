"""Embedding providers.

Two implementations behind one small interface:

* :class:`HashEmbedder` - deterministic feature hashing. Free, offline, no
  model download, no API key. It only captures word overlap, not meaning, so treat it as
  a stand-in for demos and tests, not for real retrieval quality.
* :class:`AzureOpenAIEmbedder` - calls an Azure OpenAI embeddings deployment
  (``text-embedding-3-small`` by default) over plain HTTPS with ``httpx``.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Protocol, runtime_checkable

import httpx
import numpy as np

from sqlvec.config import ConfigError, Settings, validate_dims

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NATIVE_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider fails."""


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns a list of strings into vectors of a fixed size."""

    @property
    def dims(self) -> int:
        """Length of every vector this embedder returns."""
        ...

    @property
    def name(self) -> str:
        """Short human-readable name, stored alongside the data."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input, in order."""
        ...


def l2_normalize(vec: np.ndarray) -> np.ndarray:
    """Scale a vector to unit length (zero vectors are returned unchanged)."""
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


class HashEmbedder:
    """Deterministic bag-of-words embedder using the hashing trick.

    Each lowercase word and each adjacent word pair is hashed (BLAKE2b) to a
    bucket and a sign. The vector is the signed bucket counts, L2-normalised.
    Texts that share words end up with a high cosine similarity, which is
    enough to exercise every code path without paying for an API.
    """

    def __init__(self, dims: int = 1536, use_bigrams: bool = True) -> None:
        self._dims = validate_dims(dims)
        self._use_bigrams = use_bigrams

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def name(self) -> str:
        return f"hash-{self._dims}"

    def _features(self, text: str) -> list[str]:
        words = _TOKEN_RE.findall(text.lower())
        feats = list(words)
        if self._use_bigrams:
            feats.extend(f"{a}_{b}" for a, b in pairwise(words))
        return feats

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        return value % self._dims, (1.0 if (value >> 63) & 1 else -1.0)

    def embed_one(self, text: str) -> list[float]:
        """Embed a single string."""
        vec = np.zeros(self._dims, dtype=np.float32)
        for feat in self._features(text):
            idx, sign = self._bucket(feat)
            vec[idx] += sign
        return l2_normalize(vec).astype(np.float32).tolist()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class AzureOpenAIEmbedder:
    """Embeddings from an Azure OpenAI deployment.

    Uses the REST API directly:
    ``POST {endpoint}/openai/deployments/{deployment}/embeddings?api-version=...``
    with an ``api-key`` header. Requests are batched and retried on HTTP 429
    and 5xx, honouring ``Retry-After`` when the service sends it.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str = "text-embedding-3-small",
        api_version: str = "2024-10-21",
        dims: int = 1536,
        batch_size: int = 64,
        max_retries: int = 5,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not endpoint or not api_key:
            raise ConfigError("Azure OpenAI endpoint and API key are required")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.api_version = api_version
        self._dims = validate_dims(dims)
        self.batch_size = batch_size
        self.max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout)
        self._sleep = sleep
        self.total_tokens = 0

    @property
    def dims(self) -> int:
        return self._dims

    @property
    def name(self) -> str:
        return f"azure-openai:{self.deployment}"

    @property
    def url(self) -> str:
        """Full embeddings URL for this deployment (without query string)."""
        return f"{self.endpoint}/openai/deployments/{self.deployment}/embeddings"

    def _payload(self, batch: Sequence[str]) -> dict[str, object]:
        payload: dict[str, object] = {"input": list(batch)}
        # text-embedding-3-* can shorten vectors server side. Only send the
        # parameter when it differs from the model's native size.
        native = _NATIVE_DIMS.get(self.deployment)
        if native is None or native != self._dims:
            payload["dimensions"] = self._dims
        return payload

    def _post(self, batch: Sequence[str]) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                resp = self._client.post(
                    self.url,
                    params={"api-version": self.api_version},
                    headers={"api-key": self.api_key, "content-type": "application/json"},
                    json=self._payload(batch),
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise EmbeddingError(f"Azure OpenAI request failed: {exc}") from exc
                self._sleep(min(2**attempt, 30))
                attempt += 1
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.max_retries:
                    raise EmbeddingError(
                        f"Azure OpenAI returned {resp.status_code} after {attempt} retries"
                    )
                retry_after = resp.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self._sleep(min(delay, 60.0))
                attempt += 1
                continue

            if resp.status_code >= 400:
                raise EmbeddingError(f"Azure OpenAI returned {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            data = sorted(body.get("data", []), key=lambda item: item["index"])
            if len(data) != len(batch):
                raise EmbeddingError(
                    f"expected {len(batch)} embeddings, got {len(data)} from Azure OpenAI"
                )
            vectors = [item["embedding"] for item in data]
            for vec in vectors:
                if len(vec) != self._dims:
                    raise EmbeddingError(
                        f"deployment returned {len(vec)} dims but sqlvec expects {self._dims}; "
                        "set SQLVEC_EMBED_DIMS to match"
                    )
            self.total_tokens += int(body.get("usage", {}).get("total_tokens", 0))
            return vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._post(texts[i : i + self.batch_size]))
        return out


def get_embedder(settings: Settings, client: httpx.Client | None = None) -> Embedder:
    """Build the embedder selected by ``SQLVEC_EMBED_PROVIDER``."""
    if settings.embed_provider == "hash":
        return HashEmbedder(settings.embed_dims)
    if settings.embed_provider == "azure":
        if not settings.has_azure_credentials:
            raise ConfigError(
                "SQLVEC_EMBED_PROVIDER=azure needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY"
            )
        return AzureOpenAIEmbedder(
            endpoint=settings.azure_endpoint or "",
            api_key=settings.azure_api_key or "",
            deployment=settings.azure_embed_deployment,
            api_version=settings.azure_api_version,
            dims=settings.embed_dims,
            client=client,
        )
    raise ConfigError(f"unknown embedding provider {settings.embed_provider!r}")
