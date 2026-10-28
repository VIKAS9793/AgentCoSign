"""The app's own timed work (expiry, holds, PayPal status, reconciliation) in
each visitor's behind-the-scenes panel.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| A timed job reports steps for a visitor | Its steps, then one label naming it |
| Two visitor workspaces | Each record holds only its own workspace's work |
| A pass where nothing happens | No steps and no label |
| Not the hosted demo | Nothing is traced, and the work still runs |
| A job fails | Logged; the other jobs and the next workspace still run |
| After each workspace | The step record is switched back |
"""

from typing import Any

import pytest

from agentcosign.core import trace
from agentcosign.core.config import DemoSettings
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.main import _tend_all
from agentcosign.services.workspaces import Workspaces

LIMITS = DemoSettings(
    max_workspaces=3,
    setups_per_address_per_day=2,
    scenarios_per_workspace_per_day=15,
    message_max_chars=500,
)
LABEL = "Automatic check"


class _Gateway:
    """Timed jobs that report a step when they have something to do."""

    def __init__(self, name: str, busy: tuple[str, ...] = (), fails: str = "") -> None:
        self.name = name
        self.busy = busy
        self.fails = fails
        self.ran: list[str] = []

    def _job(self, job: str) -> None:
        self.ran.append(job)
        if job == self.fails:
            raise PayPalClientError("down", {"retryable": True})
        if job in self.busy:
            trace.emit("paypal", f"{self.name} {job}")

    def expire(self) -> list[str]:
        self._job("expire")
        return []

    async def release_due(self) -> list[str]:
        self._job("release_due")
        return []

    async def settle_pending(self) -> list[str]:
        self._job("settle_pending")
        return []

    async def finish_unfinished(self) -> list[str]:
        self._job("finish_unfinished")
        return []

    async def reconcile_if_due(self) -> Any:
        self._job("reconcile_if_due")
        return None

    async def check_records_if_due(self) -> Any:
        self._job("check_records_if_due")
        return None


def _workspaces(demo: bool, *visitors: _Gateway) -> tuple[Workspaces[Any], list[str]]:
    queue = list(visitors)
    registry: Workspaces[Any] = Workspaces(
        default=_Gateway("owner"),
        build=lambda _: queue.pop(0),
        demo=demo,
        limits=LIMITS,
    )
    opened = [registry.open(f"{n}.0.0.1") for n in range(len(visitors))] if demo else []
    return registry, opened


def _titles(registry: Workspaces[Any], workspace_id: str) -> list[str]:
    record = registry.trace(workspace_id)
    assert record is not None
    return [step.title for step in record.after(0)]


async def test_a_visitors_timed_work_shows_in_their_panel_then_a_label() -> None:
    registry, [visitor] = _workspaces(True, _Gateway("a", busy=("reconcile_if_due",)))
    await _tend_all(registry)
    steps = registry.trace(visitor).after(0)  # type: ignore[union-attr]
    assert [s.title for s in steps] == ["a reconcile_if_due", steps[-1].title]
    assert steps[-1].title.startswith(LABEL)
    assert steps[-1].detail["ran"] == "reconciliation with PayPal"


@pytest.mark.edge
async def test_each_workspace_sees_only_its_own_timed_work() -> None:
    registry, [a, b] = _workspaces(
        True, _Gateway("a", busy=("expire",)), _Gateway("b", busy=("settle_pending",))
    )
    await _tend_all(registry)
    assert _titles(registry, a)[0] == "a expire"
    assert _titles(registry, b)[0] == "b settle_pending"
    assert not any(t.startswith("b ") for t in _titles(registry, a))
    assert not any(t.startswith("a ") for t in _titles(registry, b))
    assert _titles(registry, "") == []  # the owner's workspace had nothing to do


@pytest.mark.edge
async def test_a_quiet_pass_adds_nothing() -> None:
    registry, [visitor] = _workspaces(True, _Gateway("a"))
    await _tend_all(registry)
    assert _titles(registry, visitor) == []


@pytest.mark.edge
async def test_outside_the_demo_nothing_is_traced_and_the_work_still_runs() -> None:
    registry, _ = _workspaces(False)
    owner = registry.default
    owner.busy = ("expire",)
    await _tend_all(registry)
    assert "expire" in owner.ran
    assert _titles(registry, "") == []


@pytest.mark.edge
async def test_a_failing_job_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _Gateway("a", busy=("settle_pending",), fails="release_due")
    second = _Gateway("b", busy=("expire",))
    registry, [a, b] = _workspaces(True, first, second)
    await _tend_all(registry)
    assert first.ran[-1] == "check_records_if_due"
    assert "check_records_if_due" in second.ran
    assert _titles(registry, a)[0] == "a settle_pending"
    assert _titles(registry, b)[0] == "b expire"
    assert "will retry" in caplog.text


@pytest.mark.edge
async def test_the_step_record_is_switched_back_after_the_pass() -> None:
    registry, _ = _workspaces(True, _Gateway("a", busy=("expire",)))
    assert not trace.active()
    await _tend_all(registry)
    assert not trace.active()
