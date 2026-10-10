"""Azure SQL Database free offer math: how many vCore-seconds does a demo burn?

The free offer (Microsoft Learn, "Try Azure SQL Database for free", checked
2026-10-10) gives each of up to 10 General Purpose serverless databases per
subscription, every month, for the life of the subscription:

* 100,000 vCore-seconds of compute
* 32 GB of data and 32 GB of backup storage

With "auto-pause until next month" (the default, and the setting sqlvec's
Bicep uses) the database simply pauses when the allowance runs out. Max 4
vCores in that mode.

Serverless billing (Microsoft Learn, "serverless tier billing"):

    billed vCores per second = max(min vCores, vCores used,
                                   min memory GB / 3, memory used GB / 3)

and the database keeps billing at that minimum while it is online but idle,
until the auto-pause delay (15 minutes minimum, 60 minutes default for
General Purpose) has passed. That idle tail is usually the biggest line.

For GP_S_Gen5_2 with min 0.5 vCores, min memory is 2.05 GB, so the floor is
max(0.5, 2.05 / 3) = 0.683 vCores. An hour of idle-before-pause is ~2,460
vCore-seconds.

**Prices are reference values. Verify them in the Azure pricing calculator
before relying on any dollar figure.** The vCore-second price below is the
example rate Microsoft's billing page uses in its worked example, not a
quote for your region.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

FREE_VCORE_SECONDS = 100_000
FREE_DATA_GB = 32
FREE_MAX_VCORES_AUTOPAUSE = 4
MIN_AUTOPAUSE_DELAY_MIN = 15
DEFAULT_AUTOPAUSE_DELAY_MIN = 60

# Min memory per serverless GP Gen5 SKU (GB), from the vCore resource limits page.
MIN_MEMORY_GB = {"GP_S_Gen5_1": 2.02, "GP_S_Gen5_2": 2.05, "GP_S_Gen5_4": 2.10}
MAX_VCORES = {"GP_S_Gen5_1": 1, "GP_S_Gen5_2": 2, "GP_S_Gen5_4": 4}

# Reference prices (USD). VERIFY before use.
PRICE_PER_VCORE_SECOND = 0.000145  # example rate in Microsoft's serverless billing doc
PRICE_EMBED_PER_1M_TOKENS = 0.02  # text-embedding-3-small, Azure OpenAI pay-as-you-go

PRICES_NOTE = (
    "Prices are reference values (USD), not quotes: verify at "
    "https://azure.microsoft.com/pricing/calculator/ before relying on them."
)


@dataclass(frozen=True)
class Usage:
    """How the database will be used in a month."""

    sessions: int = 20
    active_minutes: float = 30.0
    active_vcores: float = 1.0
    autopause_delay_min: int = DEFAULT_AUTOPAUSE_DELAY_MIN
    sku: str = "GP_S_Gen5_2"
    min_vcores: float = 0.5
    chunks: int = 10_000
    dims: int = 1536
    tokens_per_chunk: int = 250
    queries: int = 1_000
    tokens_per_query: int = 20

    def validate(self) -> None:
        if self.sku not in MAX_VCORES:
            raise ValueError(f"sku must be one of {sorted(MAX_VCORES)}")
        if self.sessions < 0 or self.active_minutes < 0 or self.chunks < 0 or self.queries < 0:
            raise ValueError("counts and minutes must be >= 0")
        if self.autopause_delay_min < MIN_AUTOPAUSE_DELAY_MIN:
            raise ValueError(
                f"the auto-pause delay can't be below {MIN_AUTOPAUSE_DELAY_MIN} minutes "
                "for General Purpose serverless"
            )
        if not 0 < self.min_vcores <= MAX_VCORES[self.sku]:
            raise ValueError("min_vcores must be > 0 and <= the SKU's max vCores")
        if not 0 <= self.active_vcores <= MAX_VCORES[self.sku]:
            raise ValueError("active_vcores must be between 0 and the SKU's max vCores")
        if self.dims < 1:
            raise ValueError("dims must be >= 1")


@dataclass(frozen=True)
class Estimate:
    """Monthly estimate for one :class:`Usage`."""

    floor_vcores: float
    billed_active_vcores: float
    active_vcore_seconds: float
    idle_vcore_seconds: float
    total_vcore_seconds: float
    free_allowance: int
    percent_of_free: float
    sessions_that_fit: int
    overage_vcore_seconds: float
    overage_usd_if_billed: float
    vector_data_gb: float
    fits_free_storage: bool
    embed_tokens: int
    embed_usd: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def floor_vcores(sku: str, min_vcores: float) -> float:
    """What an online-but-idle database bills per second: max(min vCores, min memory / 3)."""
    return max(min_vcores, MIN_MEMORY_GB[sku] / 3.0)


def session_vcore_seconds(u: Usage) -> tuple[float, float, float]:
    """(billed vCores while active, active vCore-s, idle-tail vCore-s) for one session."""
    floor = floor_vcores(u.sku, u.min_vcores)
    billed_active = max(floor, u.active_vcores)
    return (
        billed_active,
        billed_active * u.active_minutes * 60.0,
        floor * u.autopause_delay_min * 60.0,
    )


def vector_storage_gb(chunks: int, dims: int) -> float:
    """Raw float32 vector payload: chunks x dims x 4 bytes.

    Excludes the chunk text, row overhead, any per-vector header (not
    documented that I could find) and the DiskANN index. A lower bound.
    """
    return chunks * dims * 4 / 1e9


def estimate(u: Usage) -> Estimate:
    """Monthly vCore-seconds against the free allowance, plus embedding cost."""
    u.validate()
    billed_active, active_s, idle_s = session_vcore_seconds(u)
    total_active = active_s * u.sessions
    total_idle = idle_s * u.sessions
    total = total_active + total_idle
    per_session = active_s + idle_s
    fit = math.floor(FREE_VCORE_SECONDS / per_session) if per_session > 0 else 0
    overage = max(0.0, total - FREE_VCORE_SECONDS)
    vec_gb = vector_storage_gb(u.chunks, u.dims)
    tokens = u.chunks * u.tokens_per_chunk + u.queries * u.tokens_per_query
    return Estimate(
        floor_vcores=round(floor_vcores(u.sku, u.min_vcores), 4),
        billed_active_vcores=round(billed_active, 4),
        active_vcore_seconds=round(total_active, 1),
        idle_vcore_seconds=round(total_idle, 1),
        total_vcore_seconds=round(total, 1),
        free_allowance=FREE_VCORE_SECONDS,
        percent_of_free=round(100.0 * total / FREE_VCORE_SECONDS, 1),
        sessions_that_fit=fit,
        overage_vcore_seconds=round(overage, 1),
        overage_usd_if_billed=round(overage * PRICE_PER_VCORE_SECOND, 2),
        vector_data_gb=round(vec_gb, 3),
        fits_free_storage=vec_gb < FREE_DATA_GB,
        embed_tokens=tokens,
        embed_usd=round(tokens / 1_000_000 * PRICE_EMBED_PER_1M_TOKENS, 4),
    )


def delay_table(u: Usage, delays: tuple[int, ...] = (15, 30, 60)) -> list[tuple[int, Estimate]]:
    """Same usage at several auto-pause delays: the cheapest knob you have."""
    out = []
    for d in delays:
        out.append((d, estimate(Usage(**{**asdict(u), "autopause_delay_min": d}))))
    return out


def to_markdown(u: Usage, e: Estimate) -> str:
    """Human-readable breakdown."""
    lines = [
        f"Azure SQL free offer: {FREE_VCORE_SECONDS:,} vCore-seconds and {FREE_DATA_GB} GB "
        "per database per month (auto-pause when used up).",
        "",
        f"Usage: {u.sessions} sessions/month x {u.active_minutes:g} min active at "
        f"~{u.active_vcores:g} vCores, auto-pause delay {u.autopause_delay_min} min, "
        f"{u.sku} (min {u.min_vcores:g} vCores).",
        "",
        "| line | vCore-seconds |",
        "|---|---:|",
        f"| active (billed at {e.billed_active_vcores:g} vCores) | {e.active_vcore_seconds:,.0f} |",
        f"| idle until auto-pause (floor {e.floor_vcores:g} vCores) | "
        f"{e.idle_vcore_seconds:,.0f} |",
        f"| **total** | **{e.total_vcore_seconds:,.0f}** |",
        f"| free allowance | {e.free_allowance:,} |",
        "",
        f"That's {e.percent_of_free:g}% of the monthly allowance; about "
        f"{e.sessions_that_fit} sessions like this fit in it.",
    ]
    if e.overage_vcore_seconds > 0:
        lines.append(
            f"Over by {e.overage_vcore_seconds:,.0f} vCore-seconds. With auto-pause the "
            "database just pauses until the 1st. If you had switched it to 'continue "
            f"with charges', that would be about ${e.overage_usd_if_billed:.2f} (verify)."
        )
    lines += [
        "",
        f"Vectors: {u.chunks:,} chunks x {u.dims} dims x 4 bytes = {e.vector_data_gb:g} GB "
        f"raw ({'fits' if e.fits_free_storage else 'does NOT fit'} in {FREE_DATA_GB} GB, "
        "before text, overhead and the DiskANN index).",
        f"Embeddings: ~{e.embed_tokens:,} tokens with text-embedding-3-small = "
        f"${e.embed_usd:.4f} (verify).",
        "",
        PRICES_NOTE,
    ]
    return "\n".join(lines)
