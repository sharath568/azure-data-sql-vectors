from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from sqlvec.config import ConfigError, Settings
from sqlvec.embeddings import (
    AzureOpenAIEmbedder,
    Embedder,
    EmbeddingError,
    HashEmbedder,
    get_embedder,
)

ENDPOINT = "https://unit-test.openai.azure.com/"


def cosine(a: list[float], b: list[float]) -> float:
    return float(np.dot(a, b))


# --- HashEmbedder -----------------------------------------------------------


def test_hash_embedder_is_deterministic_and_normalised() -> None:
    e = HashEmbedder(dims=256)
    a1, a2 = e.embed(["stop the server when idle", "stop the server when idle"])
    assert a1 == a2
    assert len(a1) == 256
    assert np.linalg.norm(a1) == pytest.approx(1.0, abs=1e-5)


def test_hash_embedder_similar_texts_score_higher() -> None:
    e = HashEmbedder(dims=1536)
    q, near, far = e.embed(
        [
            "how do I stop the database server",
            "you can stop the database server from the CLI",
            "bananas are rich in potassium",
        ]
    )
    assert cosine(q, near) > cosine(q, far)


def test_hash_embedder_empty_text_is_zero_vector() -> None:
    vec = HashEmbedder(dims=32).embed_one("")
    assert vec == [0.0] * 32


def test_hash_embedder_is_case_insensitive() -> None:
    e = HashEmbedder(dims=64)
    assert e.embed_one("DiskANN Index") == e.embed_one("diskann index")


def test_hash_embedder_satisfies_protocol() -> None:
    e = HashEmbedder(dims=8)
    assert isinstance(e, Embedder)
    assert e.name == "hash-8"


def test_hash_embedder_rejects_bad_dims() -> None:
    with pytest.raises(ConfigError):
        HashEmbedder(dims=0)


# --- AzureOpenAIEmbedder (mocked HTTP) --------------------------------------


def _fake_vectors(n: int, dims: int) -> list[dict[str, object]]:
    return [{"index": i, "embedding": [float(i)] * dims} for i in range(n)]


def make_client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_azure_embedder_request_shape_and_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        data = _fake_vectors(len(body["input"]), 1536)
        data.reverse()  # service may return out of order; client must sort
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 7}})

    e = AzureOpenAIEmbedder(ENDPOINT, "key", client=make_client(handler))
    out = e.embed(["a", "b", "c"])
    assert [v[0] for v in out] == [0.0, 1.0, 2.0]
    req = seen[0]
    assert req.url.path == "/openai/deployments/text-embedding-3-small/embeddings"
    assert req.url.params["api-version"] == "2024-10-21"
    assert req.headers["api-key"] == "key"
    assert "dimensions" not in json.loads(req.content)  # native size, not sent
    assert e.total_tokens == 7


def test_azure_embedder_sends_dimensions_when_shortened() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"data": _fake_vectors(len(body["input"]), 512)})

    e = AzureOpenAIEmbedder(ENDPOINT, "key", dims=512, client=make_client(handler))
    e.embed(["x"])
    assert bodies[0]["dimensions"] == 512


def test_azure_embedder_batches() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        sizes.append(n)
        return httpx.Response(200, json={"data": _fake_vectors(n, 4)})

    e = AzureOpenAIEmbedder(ENDPOINT, "k", dims=4, batch_size=3, client=make_client(handler))
    assert len(e.embed([str(i) for i in range(7)])) == 7
    assert sizes == [3, 3, 1]


def test_azure_embedder_retries_on_429_with_retry_after() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"data": _fake_vectors(1, 4)})

    e = AzureOpenAIEmbedder(ENDPOINT, "k", dims=4, client=make_client(handler), sleep=sleeps.append)
    e.embed(["x"])
    assert calls["n"] == 3
    assert sleeps == [2.0, 2.0]


def test_azure_embedder_gives_up_after_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    e = AzureOpenAIEmbedder(
        ENDPOINT, "k", dims=4, max_retries=2, client=make_client(handler), sleep=lambda _s: None
    )
    with pytest.raises(EmbeddingError, match="503"):
        e.embed(["x"])


def test_azure_embedder_client_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    e = AzureOpenAIEmbedder(ENDPOINT, "k", dims=4, client=make_client(handler))
    with pytest.raises(EmbeddingError, match="401"):
        e.embed(["x"])
    assert calls["n"] == 1


def test_azure_embedder_checks_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": _fake_vectors(1, 3)})

    e = AzureOpenAIEmbedder(ENDPOINT, "k", dims=4, client=make_client(handler))
    with pytest.raises(EmbeddingError, match="SQLVEC_EMBED_DIMS"):
        e.embed(["x"])


def test_azure_embedder_retries_transport_errors() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"data": _fake_vectors(1, 4)})

    e = AzureOpenAIEmbedder(
        ENDPOINT, "k", dims=4, client=make_client(handler), sleep=lambda _s: None
    )
    assert len(e.embed(["x"])) == 1


def test_azure_embedder_requires_credentials() -> None:
    with pytest.raises(ConfigError):
        AzureOpenAIEmbedder("", "key")
    with pytest.raises(ConfigError):
        AzureOpenAIEmbedder(ENDPOINT, "")


# --- factory ----------------------------------------------------------------


def test_get_embedder_hash_by_default() -> None:
    e = get_embedder(Settings.from_env({"SQLVEC_EMBED_DIMS": "64"}))
    assert isinstance(e, HashEmbedder)
    assert e.dims == 64


def test_get_embedder_azure_requires_credentials() -> None:
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        get_embedder(Settings.from_env({"SQLVEC_EMBED_PROVIDER": "azure"}))


def test_get_embedder_azure_configured() -> None:
    settings = Settings.from_env(
        {
            "SQLVEC_EMBED_PROVIDER": "azure",
            "AZURE_OPENAI_ENDPOINT": ENDPOINT,
            "AZURE_OPENAI_API_KEY": "k",
            "AZURE_OPENAI_EMBED_DEPLOYMENT": "my-embed",
        }
    )
    e = get_embedder(settings)
    assert isinstance(e, AzureOpenAIEmbedder)
    assert e.name == "azure-openai:my-embed"
    assert e.url.endswith("/openai/deployments/my-embed/embeddings")
