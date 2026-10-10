"""Traffic protection core: buckets, penalty box, pools, identity, capacity."""

import pytest

from agentcosign.core.config import LimitSettings
from agentcosign.core.limits import (
    CapacityMode,
    Tier,
    TrafficGuard,
    WorkKind,
    resolve_client_key,
)

T0 = 1_000_000  # milliseconds; never 0 so "unset" sentinels cannot hide bugs


def make_limits(**overrides: object) -> LimitSettings:
    base: dict[str, object] = {
        "trusted_proxy_hops": 1,
        "client_ip_header": "",
        "expose_client_diagnostics": True,
        "max_body_bytes": 65_536,
        "max_concurrent_requests": 50,
        "reviewer_reserved_concurrency": 0,
        "max_tracked_clients": 100,
        "exempt_paths": ["/health"],
        "public": {
            "general": {"capacity": 3, "refill_seconds": 10},
            "live_llm": {"capacity": 2, "refill_seconds": 60},
            "live_execution": {"capacity": 1, "refill_seconds": 600},
        },
        "reviewer": {
            "general": {"capacity": 10, "refill_seconds": 1},
            "live_llm": {"capacity": 5, "refill_seconds": 10},
            "live_execution": {"capacity": 3, "refill_seconds": 60},
        },
        "live_llm_budget": {"provider_daily_limit": 100, "public": 3, "reviewer": 5},
        "live_execution_budget": {"public": 1, "reviewer": 2},
        "provider_low_percent": 10,
        "penalty": {
            "strikes": 3,
            "strike_window_seconds": 60,
            "block_seconds": 30,
            "max_block_seconds": 120,
        },
    }
    base.update(overrides)
    return LimitSettings.model_validate(base)


def admit(guard: TrafficGuard, now_ms: int, key: str = "1.1.1.1"):  # noqa: ANN201
    return guard.admit(key, Tier.PUBLIC, "/api/x", now_ms)


# --- client identity ------------------------------------------------------------
@pytest.mark.parametrize(
    ("peer", "forwarded", "hops", "expected"),
    [
        ("10.0.0.1", "203.0.113.5", 1, "203.0.113.5"),
        # a caller-supplied leading entry is ignored; the proxy-appended one wins
        ("10.0.0.1", "6.6.6.6, 203.0.113.5", 1, "203.0.113.5"),
        ("10.0.0.1", "203.0.113.5, 10.1.1.1", 2, "203.0.113.5"),
        # the hosted chain: client, edge, internal hop (three trusted hops)
        ("10.24.1.1", "203.0.113.5, 172.68.1.1, 10.24.1.1", 3, "203.0.113.5"),
        ("10.24.1.1", "6.6.6.6, 203.0.113.5, 172.68.1.1, 10.24.1.1", 3, "203.0.113.5"),
        # internal addresses are shared hops, never identities
        ("10.0.0.1", None, 1, "unknown"),
        ("10.0.0.1", "10.2.3.4", 1, "unknown"),
        ("10.0.0.1", "203.0.113.5", 0, "unknown"),
        ("10.0.0.1", "garbage", 1, "unknown"),
        (None, None, 1, "unknown"),
        # a direct caller with no proxy in front is identified by its own address
        ("198.51.100.9", None, 1, "198.51.100.9"),
        ("10.0.0.1", "2001:db8:1:2:aaaa:bbbb:cccc:dddd", 1, "2001:db8:1:2::/64"),
        ("10.0.0.1", "2001:db8:1:2:1:1:1:1", 1, "2001:db8:1:2::/64"),
    ],
)
def test_resolve_client_key(
    peer: str | None, forwarded: str | None, hops: int, expected: str
) -> None:
    assert resolve_client_key(peer, forwarded, hops) == expected


@pytest.mark.parametrize(
    ("connecting", "forwarded", "hops", "expected"),
    [
        # the edge header wins and does not depend on the hop count
        ("203.0.113.5", "1.1.1.1, 2.2.2.2, 10.1.1.1", 1, "203.0.113.5"),
        ("203.0.113.5", None, 0, "203.0.113.5"),
        # an unusable header falls back to the forwarding chain
        (None, "203.0.113.9, 172.68.1.1, 10.1.1.1", 3, "203.0.113.9"),
        ("10.9.9.9", "203.0.113.9, 172.68.1.1, 10.1.1.1", 3, "203.0.113.9"),
        ("not-an-ip", None, 3, "unknown"),
    ],
)
def test_edge_header_is_preferred_then_chain_then_unknown(
    connecting: str | None, forwarded: str | None, hops: int, expected: str
) -> None:
    assert resolve_client_key("10.0.0.1", forwarded, hops, connecting) == expected


def test_unidentified_callers_are_not_limited_per_client_or_penalised() -> None:
    guard = TrafficGuard(make_limits())
    for _ in range(200):
        result = guard.admit("unknown", Tier.PUBLIC, "/api/x", T0)
        assert result.allowed
        guard.release()
    guard.penalize("unknown", T0)  # must not create a shared penalty
    assert guard.admit("unknown", Tier.PUBLIC, "/api/x", T0).allowed


def test_unidentified_callers_still_hit_the_global_cap_and_pools() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=2))
    assert guard.admit("unknown", Tier.PUBLIC, "/api/x", T0).allowed
    assert guard.admit("unknown", Tier.PUBLIC, "/api/x", T0).allowed
    assert guard.admit("unknown", Tier.PUBLIC, "/api/x", T0).status == 503
    pool = [
        guard.request_live("unknown", Tier.PUBLIC, WorkKind.LIVE_LLM, T0).live
        for _ in range(5)
    ]
    assert pool == [True, True, True, False, False]  # the public pool holds 3


# --- buckets and penalty box ----------------------------------------------------
def test_bucket_allows_burst_then_rejects_with_retry_after() -> None:
    guard = TrafficGuard(make_limits())
    results = [admit(guard, T0) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[3].status == 429
    assert results[3].reason == "rate_limited"
    assert 1 <= results[3].retry_after_seconds <= 10


def test_bucket_refills_over_time() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=50))
    for _ in range(3):
        admit(guard, T0)
    assert not admit(guard, T0).allowed
    assert admit(guard, T0 + 10_000).allowed  # one token per 10 s
    assert not admit(guard, T0 + 10_000).allowed


def test_bucket_never_exceeds_capacity_after_long_idle() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=50))
    admit(guard, T0)
    later = T0 + 10 * 60 * 60 * 1000
    allowed = [admit(guard, later).allowed for _ in range(5)]
    assert allowed == [True, True, True, False, False]


def test_clients_are_isolated() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=50))
    for _ in range(3):
        admit(guard, T0, key="a")
    assert not admit(guard, T0, key="a").allowed
    assert admit(guard, T0, key="b").allowed


def test_repeated_rejections_escalate_to_a_block_that_doubles() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=50))
    for _ in range(3):
        admit(guard, T0)
    now = T0
    blocked_seconds = []
    for _ in range(2):
        result = admit(guard, now)
        for _ in range(20):  # keep hammering until the penalty box closes
            if result.reason == "blocked":
                break
            result = admit(guard, now)
        assert result.reason == "blocked"
        blocked_seconds.append(result.retry_after_seconds)
        now += result.retry_after_seconds * 1000 + 1
    assert blocked_seconds[1] > blocked_seconds[0]
    assert blocked_seconds[0] <= 30
    assert blocked_seconds[1] <= 120


def test_exempt_paths_bypass_everything() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=1))
    guard.admit("a", Tier.PUBLIC, "/api/x", T0)  # fills the single slot
    for _ in range(50):
        result = guard.admit("a", Tier.PUBLIC, "/health", T0)
        assert result.allowed
        assert not result.counted


def test_inflight_cap_returns_fast_503_and_recovers_on_release() -> None:
    guard = TrafficGuard(make_limits(max_concurrent_requests=2))
    first = guard.admit("a", Tier.PUBLIC, "/api/x", T0)
    second = guard.admit("b", Tier.PUBLIC, "/api/x", T0)
    third = guard.admit("c", Tier.PUBLIC, "/api/x", T0)
    assert (first.allowed, second.allowed) == (True, True)
    assert (third.allowed, third.status) == (False, 503)
    guard.release()
    assert guard.admit("c", Tier.PUBLIC, "/api/x", T0).allowed
    assert guard.inflight == 2


def test_reviewers_keep_slots_when_the_public_has_filled_the_rest() -> None:
    guard = TrafficGuard(
        make_limits(max_concurrent_requests=4, reviewer_reserved_concurrency=2)
    )
    public = [guard.admit(f"p{i}", Tier.PUBLIC, "/api/x", T0).allowed for i in range(4)]
    assert public == [True, True, False, False]  # public may use 2 of 4
    reviewer = [
        guard.admit(f"r{i}", Tier.REVIEWER, "/api/x", T0).allowed for i in range(3)
    ]
    assert reviewer == [True, True, False]  # reviewers may use the rest


def test_reserve_must_leave_room_for_the_public() -> None:
    with pytest.raises(ValueError):
        make_limits(max_concurrent_requests=4, reviewer_reserved_concurrency=4)


def test_release_never_goes_negative() -> None:
    guard = TrafficGuard(make_limits())
    guard.release()
    guard.release()
    assert guard.inflight == 0


def test_tracked_clients_are_bounded() -> None:
    guard = TrafficGuard(
        make_limits(max_tracked_clients=100, max_concurrent_requests=1)
    )
    for index in range(1_000):
        guard.admit(f"10.0.{index // 250}.{index % 250}", Tier.PUBLIC, "/api/x", T0)
        guard.release()
    assert len(guard._clients) <= 100  # noqa: SLF001 -- asserting the memory bound


# --- live calls and daily pools -------------------------------------------------
def test_public_pool_is_finite_and_never_touches_the_reviewer_pool() -> None:
    guard = TrafficGuard(make_limits())
    decisions = []
    for index in range(5):  # public pool holds 3; vary the client to skip buckets
        decisions.append(
            guard.request_live(f"c{index}", Tier.PUBLIC, WorkKind.LIVE_LLM, T0).live
        )
    assert decisions == [True, True, True, False, False]
    reviewer = guard.request_live("j", Tier.REVIEWER, WorkKind.LIVE_LLM, T0)
    assert reviewer.live


def test_reviewer_uses_own_pool_then_falls_back_to_public_pool() -> None:
    guard = TrafficGuard(make_limits())
    taken = [
        guard.request_live(f"j{i}", Tier.REVIEWER, WorkKind.LIVE_LLM, T0).live
        for i in range(9)
    ]
    assert taken == [True] * 8 + [False]  # 5 reviewer + 3 public, then nothing


def test_client_bucket_limits_live_calls_before_the_pool() -> None:
    guard = TrafficGuard(make_limits())
    results = [
        guard.request_live("same", Tier.PUBLIC, WorkKind.LIVE_LLM, T0) for _ in range(3)
    ]
    assert [r.live for r in results] == [True, True, False]
    assert results[2].reason == "client_quota"


def test_pools_reset_after_24_hours() -> None:
    guard = TrafficGuard(make_limits())
    for index in range(3):
        guard.request_live(f"c{index}", Tier.PUBLIC, WorkKind.LIVE_LLM, T0)
    assert not guard.request_live("z", Tier.PUBLIC, WorkKind.LIVE_LLM, T0).live
    later = T0 + 24 * 60 * 60 * 1000
    assert guard.request_live("z", Tier.PUBLIC, WorkKind.LIVE_LLM, later).live


def test_a_refusal_is_a_replay_decision_not_an_error() -> None:
    guard = TrafficGuard(make_limits())
    decision = guard.request_live("a", Tier.PUBLIC, WorkKind.GENERAL, T0)
    assert decision.live is False
    assert decision.reason == "not_a_live_kind"


def test_execution_pool_is_separate_from_model_pool() -> None:
    guard = TrafficGuard(make_limits())
    assert guard.request_live("a", Tier.PUBLIC, WorkKind.LIVE_EXECUTION, T0).live
    assert not guard.request_live("b", Tier.PUBLIC, WorkKind.LIVE_EXECUTION, T0).live
    assert guard.request_live("c", Tier.PUBLIC, WorkKind.LIVE_LLM, T0).live


# --- provider quota and capacity mode --------------------------------------------
def test_low_provider_quota_moves_public_to_replay_but_keeps_reviewers_live() -> None:
    guard = TrafficGuard(make_limits())
    assert guard.capacity_mode(T0) is CapacityMode.NORMAL
    guard.observe_provider_quota(remaining=5, limit=100)
    assert guard.capacity_mode(T0) is CapacityMode.PUBLIC_REPLAY
    assert not guard.request_live("p", Tier.PUBLIC, WorkKind.LIVE_LLM, T0).live
    assert guard.request_live("j", Tier.REVIEWER, WorkKind.LIVE_LLM, T0).live


def test_exhausted_provider_means_replay_only_for_everyone() -> None:
    guard = TrafficGuard(make_limits())
    guard.observe_provider_quota(remaining=0, limit=100)
    assert guard.capacity_mode(T0) is CapacityMode.REPLAY_ONLY
    assert not guard.request_live("j", Tier.REVIEWER, WorkKind.LIVE_LLM, T0).live


def test_provider_recovery_restores_normal_mode() -> None:
    guard = TrafficGuard(make_limits())
    guard.observe_provider_quota(remaining=0, limit=100)
    guard.observe_provider_quota(remaining=90, limit=100)
    assert guard.capacity_mode(T0) is CapacityMode.NORMAL


def test_missing_quota_headers_are_ignored() -> None:
    guard = TrafficGuard(make_limits())
    guard.observe_provider_quota(remaining=None, limit=None)
    assert guard.capacity_mode(T0) is CapacityMode.NORMAL


def test_exhausting_every_pool_reports_replay_only() -> None:
    guard = TrafficGuard(make_limits())
    for index in range(8):
        guard.request_live(f"j{index}", Tier.REVIEWER, WorkKind.LIVE_LLM, T0)
    assert guard.capacity_mode(T0) is CapacityMode.REPLAY_ONLY


# --- configuration is fail-closed -------------------------------------------------
def test_pools_cannot_exceed_the_provider_limit() -> None:
    with pytest.raises(ValueError):
        make_limits(
            live_llm_budget={"provider_daily_limit": 100, "public": 60, "reviewer": 60}
        )


def test_reviewer_limits_cannot_be_stricter_than_public() -> None:
    with pytest.raises(ValueError):
        make_limits(
            reviewer={
                "general": {"capacity": 1, "refill_seconds": 1},
                "live_llm": {"capacity": 5, "refill_seconds": 10},
                "live_execution": {"capacity": 3, "refill_seconds": 60},
            }
        )


def test_exempt_paths_must_be_absolute() -> None:
    with pytest.raises(ValueError):
        make_limits(exempt_paths=["health"])
