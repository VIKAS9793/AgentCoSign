"""Traffic protection: per-client buckets, a penalty box and daily live-call pools.

Pure logic with an injected clock (integer milliseconds), no I/O, bounded memory.
The layers, from cheapest to strongest:

1. Per-client token buckets: fairness between callers.
2. A penalty box: callers that keep being rejected are blocked, doubling each time.
3. A global in-flight cap: a fast 503 instead of a queue when the instance is busy.
4. Daily pools of live model calls and live executions, split between the public
   and reviewers, so abuse by the public can never consume the reviewers' share.

Layers 3 and 4 do not depend on knowing who the caller is, so they still hold when
a client hides or fakes its address.
"""

import ipaddress
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum

from agentcosign.core.config import BucketSpec, LimitSettings, TierLimits

MS_PER_SECOND = 1_000
DAY_MS = 24 * 60 * 60 * MS_PER_SECOND
UNKNOWN_CLIENT = "unknown"
IPV6_PREFIX = 64  # one household or host rotates addresses inside a /64


class Tier(StrEnum):
    """Who is calling: the general public or a reviewer holding the token."""

    PUBLIC = "public"
    REVIEWER = "reviewer"


class WorkKind(StrEnum):
    """What a request asks for, from cheapest to most expensive."""

    GENERAL = "general"
    LIVE_LLM = "live_llm"
    LIVE_EXECUTION = "live_execution"


class CapacityMode(StrEnum):
    """What the service can currently offer live, reported honestly."""

    NORMAL = "normal"
    PUBLIC_REPLAY = "public_replay"  # public served from recordings; reviewers live
    REPLAY_ONLY = "replay_only"  # everyone served from recordings


@dataclass(frozen=True)
class Admission:
    """Result of admitting a request into the service."""

    allowed: bool
    counted: bool = False  # True when release() must be called afterwards
    stream: bool = False  # counted against the open-stream cap, not request slots
    status: int = 200
    reason: str = ""
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class LiveDecision:
    """Whether a request may use a live dependency, or must be served from replay."""

    live: bool
    reason: str = ""


@dataclass
class _Bucket:
    tokens: int
    updated_ms: int


@dataclass
class _Client:
    buckets: dict[tuple[Tier, WorkKind], _Bucket] = field(default_factory=dict)
    strike_count: int = 0
    strike_window_start_ms: int = 0
    blocked_until_ms: int = 0
    block_level: int = 0
    last_block_ms: int | None = None


@dataclass
class _Pool:
    limit: int
    used: int = 0
    window_start_ms: int | None = None


_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _external_address(
    value: str | None,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an address, returning None when invalid or on an internal network."""
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    for network in _INTERNAL_NETWORKS:
        if network.version == address.version and address in network:
            return None
    return address


def resolve_client_key(
    peer: str | None,
    forwarded_for: str | None,
    trusted_hops: int,
    connecting_ip: str | None = None,
) -> str:
    """Return a stable key for the caller, or ``unknown``.

    Order of trust: an address header set by the edge (the edge rejects requests
    that try to send it themselves), then the X-Forwarded-For entry
    ``trusted_hops`` from the right (only proxies we control append there, so
    entries a caller supplies sit further left and are ignored), then the direct
    peer. Internal-network addresses are never used as identities, because they
    are shared proxy hops that would put unrelated callers in one bucket.
    IPv6 callers are grouped by /64 so rotating inside their own range does not
    mint new identities.
    """
    address = _external_address(connecting_ip)
    if address is None and forwarded_for and trusted_hops > 0:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if len(parts) >= trusted_hops:
            address = _external_address(parts[-trusted_hops])
    if address is None and not forwarded_for:
        address = _external_address(peer)
    if address is None:
        return UNKNOWN_CLIENT
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/{IPV6_PREFIX}", strict=False))
    return str(address)


def _take(bucket: _Bucket, spec: BucketSpec, now_ms: int) -> int | None:
    """Spend one token; return None if admitted, else milliseconds until one exists."""
    refill_ms = spec.refill_seconds * MS_PER_SECOND
    gained = (now_ms - bucket.updated_ms) // refill_ms
    if gained > 0:
        bucket.tokens = min(spec.capacity, bucket.tokens + gained)
        bucket.updated_ms = (
            now_ms
            if bucket.tokens >= spec.capacity
            else bucket.updated_ms + gained * refill_ms
        )
    if bucket.tokens > 0:
        bucket.tokens -= 1
        return None
    return max(1, refill_ms - (now_ms - bucket.updated_ms))


def _seconds_up(milliseconds: int) -> int:
    return max(1, -(-milliseconds // MS_PER_SECOND))


class TrafficGuard:
    """Admission control and live-call budgeting for one process."""

    def __init__(self, limits: LimitSettings) -> None:
        self._limits = limits
        self._clients: OrderedDict[str, _Client] = OrderedDict()
        self._inflight = 0
        self._streams = 0
        self._pools: dict[tuple[WorkKind, Tier], _Pool] = {
            (WorkKind.LIVE_LLM, Tier.PUBLIC): _Pool(limits.live_llm_budget.public),
            (WorkKind.LIVE_LLM, Tier.REVIEWER): _Pool(limits.live_llm_budget.reviewer),
            (WorkKind.LIVE_EXECUTION, Tier.PUBLIC): _Pool(
                limits.live_execution_budget.public
            ),
            (WorkKind.LIVE_EXECUTION, Tier.REVIEWER): _Pool(
                limits.live_execution_budget.reviewer
            ),
        }
        self._provider_remaining_percent: int | None = None
        self._provider_exhausted = False

    # ----------------------------------------------------------------- admission

    def admit(self, client_key: str, tier: Tier, path: str, now_ms: int) -> Admission:
        """Decide whether a request may run. Call release() if ``counted``."""
        if path in self._limits.exempt_paths:
            return Admission(allowed=True)
        stream = path in self._limits.streaming_paths
        if client_key == UNKNOWN_CLIENT:
            return self._admit_unidentified(stream)
        client = self._client(client_key)
        if client.blocked_until_ms > now_ms:
            wait = client.blocked_until_ms - now_ms
            return Admission(
                False,
                status=429,
                reason="blocked",
                retry_after_seconds=_seconds_up(wait),
            )
        if self._full(tier, stream):
            return Admission(False, status=503, reason="busy", retry_after_seconds=2)
        wait_ms = self._spend(client, tier, WorkKind.GENERAL, now_ms)
        if wait_ms is not None:
            self._strike(client, now_ms)
            return Admission(
                False,
                status=429,
                reason="rate_limited",
                retry_after_seconds=_seconds_up(wait_ms),
            )
        return self._take(stream)

    def _full(self, tier: Tier, stream: bool) -> bool:
        """True when no slot of the kind this request needs is free."""
        if stream:
            return self._streams >= self._limits.max_open_streams
        ceiling = self._limits.max_concurrent_requests
        if tier is not Tier.REVIEWER:
            ceiling -= self._limits.reviewer_reserved_concurrency
        return self._inflight >= ceiling

    def _take(self, stream: bool) -> Admission:
        if stream:
            self._streams += 1
        else:
            self._inflight += 1
        return Admission(allowed=True, counted=True, stream=stream)

    def _admit_unidentified(self, stream: bool) -> Admission:
        """Admit a caller whose address could not be resolved.

        Unrelated callers would share one per-client bucket and one penalty, so
        a single abuser could block everybody. Only the global in-flight cap
        applies instead; the daily pools still bound the expensive work.
        """
        if self._full(Tier.PUBLIC, stream):
            return Admission(False, status=503, reason="busy", retry_after_seconds=2)
        return self._take(stream)

    def release(self, admission: Admission) -> None:
        """Mark one admitted request or stream as finished."""
        if admission.stream:
            self._streams = max(0, self._streams - 1)
        else:
            self._inflight = max(0, self._inflight - 1)

    def penalize(self, client_key: str, now_ms: int) -> None:
        """Count a misbehaving request (for example a wrong token) as a strike."""
        if client_key != UNKNOWN_CLIENT:
            self._strike(self._client(client_key), now_ms)

    @property
    def inflight(self) -> int:
        """Requests currently being served."""
        return self._inflight

    # --------------------------------------------------------------- live calls

    def request_live(
        self, client_key: str, tier: Tier, kind: WorkKind, now_ms: int
    ) -> LiveDecision:
        """Ask to use a live dependency; a refusal means serve from replay.

        A refusal is a normal outcome, never an error: the product stays usable
        from recorded answers when the live budget is spent.
        """
        if kind is WorkKind.GENERAL:
            return LiveDecision(live=False, reason="not_a_live_kind")
        if client_key != UNKNOWN_CLIENT:
            client = self._client(client_key)
            if self._spend(client, tier, kind, now_ms) is not None:
                return LiveDecision(live=False, reason="client_quota")
        if kind is WorkKind.LIVE_LLM and self._provider_exhausted:
            return LiveDecision(live=False, reason="provider_exhausted")
        if self._try_pool(kind, tier, now_ms):
            return LiveDecision(live=True)
        if tier is Tier.REVIEWER and self._try_pool(kind, Tier.PUBLIC, now_ms):
            return LiveDecision(live=True)
        return LiveDecision(live=False, reason="daily_budget")

    def observe_provider_quota(self, remaining: int | None, limit: int | None) -> None:
        """Feed the provider's own quota headers; they outrank local counters."""
        if remaining is None or not limit or limit <= 0:
            return
        self._provider_exhausted = remaining <= 0
        self._provider_remaining_percent = (remaining * 100) // limit

    def capacity_mode(self, now_ms: int) -> CapacityMode:
        """Summarise what can be served live right now."""
        public_ok = self._pool_available(WorkKind.LIVE_LLM, Tier.PUBLIC, now_ms)
        low = self._provider_remaining_percent
        if low is not None and low < self._limits.provider_low_percent:
            public_ok = False
        reviewer_ok = self._pool_available(WorkKind.LIVE_LLM, Tier.REVIEWER, now_ms)
        reviewer_ok = reviewer_ok or public_ok
        if self._provider_exhausted or not reviewer_ok:
            return CapacityMode.REPLAY_ONLY
        return CapacityMode.NORMAL if public_ok else CapacityMode.PUBLIC_REPLAY

    # ----------------------------------------------------------------- internals

    def _client(self, key: str) -> _Client:
        client = self._clients.get(key)
        if client is None:
            client = _Client()
            self._clients[key] = client
            while len(self._clients) > self._limits.max_tracked_clients:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(key)
        return client

    def _tier_limits(self, tier: Tier) -> TierLimits:
        return self._limits.reviewer if tier is Tier.REVIEWER else self._limits.public

    def _spend(
        self, client: _Client, tier: Tier, kind: WorkKind, now_ms: int
    ) -> int | None:
        spec: BucketSpec = getattr(self._tier_limits(tier), kind.value)
        bucket = client.buckets.get((tier, kind))
        if bucket is None:
            bucket = _Bucket(tokens=spec.capacity, updated_ms=now_ms)
            client.buckets[(tier, kind)] = bucket
        return _take(bucket, spec, now_ms)

    def _strike(self, client: _Client, now_ms: int) -> None:
        penalty = self._limits.penalty
        window_ms = penalty.strike_window_seconds * MS_PER_SECOND
        if now_ms - client.strike_window_start_ms > window_ms:
            client.strike_window_start_ms = now_ms
            client.strike_count = 0
        client.strike_count += 1
        if client.strike_count < penalty.strikes:
            return
        quiet_ms = penalty.max_block_seconds * 2 * MS_PER_SECOND
        last = client.last_block_ms
        if last is not None and now_ms - last > quiet_ms:
            client.block_level = 0
        seconds = min(
            penalty.block_seconds * (2**client.block_level), penalty.max_block_seconds
        )
        client.blocked_until_ms = now_ms + seconds * MS_PER_SECOND
        client.last_block_ms = now_ms
        client.block_level += 1
        client.strike_count = 0

    def _roll(self, pool: _Pool, now_ms: int) -> None:
        started = pool.window_start_ms
        if started is None or now_ms - started >= DAY_MS:
            pool.window_start_ms = now_ms
            pool.used = 0

    def _pool_available(self, kind: WorkKind, tier: Tier, now_ms: int) -> bool:
        pool = self._pools[(kind, tier)]
        self._roll(pool, now_ms)
        return pool.used < pool.limit

    def _try_pool(self, kind: WorkKind, tier: Tier, now_ms: int) -> bool:
        if kind is WorkKind.LIVE_LLM and tier is Tier.PUBLIC:
            low = self._provider_remaining_percent
            if low is not None and low < self._limits.provider_low_percent:
                return False
        pool = self._pools[(kind, tier)]
        self._roll(pool, now_ms)
        if pool.used >= pool.limit:
            return False
        pool.used += 1
        return True
