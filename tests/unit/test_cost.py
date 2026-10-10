from __future__ import annotations

import pytest

from sqlvec import cost


def test_floor_uses_memory_when_min_vcores_is_half() -> None:
    # GP_S_Gen5_2: min memory 2.05 GB -> 2.05 / 3 = 0.6833 vCores beats 0.5
    assert cost.floor_vcores("GP_S_Gen5_2", 0.5) == pytest.approx(2.05 / 3)
    assert cost.floor_vcores("GP_S_Gen5_2", 1.0) == 1.0


def test_session_math() -> None:
    u = cost.Usage(active_minutes=10, active_vcores=2, autopause_delay_min=15, sku="GP_S_Gen5_2")
    billed, active, idle = cost.session_vcore_seconds(u)
    assert billed == 2
    assert active == pytest.approx(2 * 10 * 60)
    assert idle == pytest.approx(2.05 / 3 * 15 * 60)


def test_active_below_floor_bills_floor() -> None:
    u = cost.Usage(active_vcores=0.1, sku="GP_S_Gen5_1")
    billed, _, _ = cost.session_vcore_seconds(u)
    assert billed == pytest.approx(2.02 / 3)


def test_estimate_against_free_allowance() -> None:
    u = cost.Usage(sessions=20, active_minutes=30, active_vcores=1.0, autopause_delay_min=60)
    e = cost.estimate(u)
    # 20 * (1.0 * 1800 + 0.68333 * 3600) = 36,000 + 49,200
    assert e.active_vcore_seconds == pytest.approx(36_000)
    assert e.idle_vcore_seconds == pytest.approx(49_200)
    assert e.total_vcore_seconds == pytest.approx(85_200)
    assert e.percent_of_free == pytest.approx(85.2)
    assert e.sessions_that_fit == 23
    assert e.overage_vcore_seconds == 0 and e.overage_usd_if_billed == 0


def test_overage_and_reference_price() -> None:
    e = cost.estimate(cost.Usage(sessions=40))
    assert e.overage_vcore_seconds == pytest.approx(40 * 4260 - 100_000)
    assert e.overage_usd_if_billed == pytest.approx(
        round((40 * 4260 - 100_000) * cost.PRICE_PER_VCORE_SECOND, 2)
    )


def test_shorter_autopause_delay_saves_vcore_seconds() -> None:
    rows = cost.delay_table(cost.Usage())
    totals = [e.total_vcore_seconds for _, e in rows]
    assert [d for d, _ in rows] == [15, 30, 60]
    assert totals == sorted(totals)


def test_storage_and_embedding_lines() -> None:
    e = cost.estimate(cost.Usage(chunks=1_000_000, dims=1536, queries=0))
    assert e.vector_data_gb == pytest.approx(6.144)
    assert e.fits_free_storage
    assert e.embed_tokens == 250_000_000
    assert e.embed_usd == pytest.approx(5.0)
    big = cost.estimate(cost.Usage(chunks=6_000_000, dims=1536))
    assert not big.fits_free_storage


@pytest.mark.parametrize(
    "kwargs",
    [
        {"autopause_delay_min": 10},
        {"sku": "GP_S_Gen5_8"},
        {"min_vcores": 3, "sku": "GP_S_Gen5_2"},
        {"active_vcores": 5, "sku": "GP_S_Gen5_4"},
        {"sessions": -1},
        {"dims": 0},
    ],
)
def test_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cost.estimate(cost.Usage(**kwargs))  # type: ignore[arg-type]


def test_markdown_mentions_verify() -> None:
    u = cost.Usage(sessions=40)
    md = cost.to_markdown(u, cost.estimate(u))
    assert "verify" in md and "100,000" in md and "auto-pause" in md
    assert "Over by" in md
