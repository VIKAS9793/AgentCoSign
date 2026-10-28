"""A live record of what the backend does for one workspace.

The hosted demo shows technical visitors every step from their first click to
the end: PayPal calls, model calls, checks, rule decisions and record entries.
Any layer reports a step with ``emit``; it is kept only when the current
request belongs to a workspace that is being traced, so outside the demo
nothing is recorded and nothing changes.

Steps carry ids, model names, timings and outcomes only: never secrets,
tokens, setup links, full PayPal payloads or unmasked customer text.
"""

import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime

MAX_STEPS = 200
OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True, slots=True)
class Step:
    """One thing the backend did, in a form safe to show."""

    seq: int
    at: str
    layer: str
    title: str
    status: str
    ms: int | None
    detail: Mapping[str, str] = field(default_factory=dict)


class Trace:
    """The most recent steps of one workspace, numbered so a reader can resume."""

    def __init__(self, limit: int = MAX_STEPS) -> None:
        self._steps: deque[Step] = deque(maxlen=limit)
        self._seq = 0
        self._lock = threading.Lock()

    def add(
        self,
        layer: str,
        title: str,
        status: str,
        ms: int | None,
        detail: Mapping[str, str],
    ) -> None:
        """Keep a step; the oldest one is dropped past the limit."""
        with self._lock:
            self._seq += 1
            self._steps.append(
                Step(
                    seq=self._seq,
                    at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                    layer=layer,
                    title=title,
                    status=status,
                    ms=ms,
                    detail=dict(detail),
                )
            )

    def after(self, seq: int) -> list[Step]:
        """Steps newer than ``seq``, oldest first."""
        with self._lock:
            return [step for step in self._steps if step.seq > seq]

    @property
    def last(self) -> int:
        """The number of the newest step (0 before any)."""
        with self._lock:
            return self._seq


_current: ContextVar[Trace | None] = ContextVar("trace", default=None)


def use(trace: Trace | None) -> Token[Trace | None]:
    """Report this request's steps into ``trace`` (None reports nowhere)."""
    return _current.set(trace)


def reset(token: Token[Trace | None]) -> None:
    """Report into whatever record was in use before ``use`` returned ``token``."""
    _current.reset(token)


def emit(
    layer: str,
    title: str,
    status: str = OK,
    ms: int | None = None,
    **detail: object,
) -> None:
    """Report a step for the current workspace, if it is being traced."""
    trace = _current.get()
    if trace is not None:
        trace.add(layer, title, status, ms, {k: str(v) for k, v in detail.items()})


def position() -> int:
    """The newest step number in the current workspace's record (0 if none)."""
    trace = _current.get()
    return 0 if trace is None else trace.last


def since(seq: int) -> list[Step]:
    """The current workspace's steps newer than ``seq`` (none if not traced)."""
    trace = _current.get()
    return [] if trace is None else trace.after(seq)


def active() -> bool:
    """Whether this request's steps are being kept."""
    return _current.get() is not None


@contextmanager
def timed(
    layer: str, title: str, on_fail: str = "", **detail: object
) -> Iterator[dict[str, object]]:
    """Report a step with its duration; a failure is reported as failed, with
    ``on_fail`` saying where things stand, and raised again. The caller may add
    details to the yielded mapping."""
    extra: dict[str, object] = {}
    started = time.monotonic()
    try:
        yield extra
    except Exception:  # guardrail: allow blind-except -- reported, then raised again
        emit(layer, title, FAIL, _elapsed(started), **detail, **extra, safe=on_fail)
        raise
    emit(layer, title, OK, _elapsed(started), **detail, **extra)


# guardrail: allow float-annotation -- monotonic seconds, not money
def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
