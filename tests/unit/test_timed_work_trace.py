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
| Timed work reports steps mid-run | The run's card counts only its own steps |
| Attack line with no refund proposed | Nothing sent; no request refused |
| A visitor's step lands while a job runs | Not counted as the job's; no label |
| A job raises something unexpected | Logged by name; other jobs and workspaces run |
| The same job fails every pass | Later jobs still run on every pass |
| Shutdown cancels a job | The cancel gets through and the pass ends |
"""

import asyncio
from typing import Any

import pytest

from agentcosign.core import trace
from agentcosign.core.config import DemoSettings
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.main import _tend_all
from agentcosign.models.policy import ScreeningVerdict
from agentcosign.services import ai_checks, run_report
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

    def __init__(
        self,
        name: str,
        busy: tuple[str, ...] = (),
        fails: str = "",
        visitor: tuple[str, ...] = (),
        raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.busy = busy
        self.fails = fails
        # jobs during which a visitor's own request reports a step
        self.visitor = visitor
        self.raises = raises or PayPalClientError("down", {"retryable": True})
        self.ran: list[str] = []

    def _job(self, job: str) -> None:
        self.ran.append(job)
        if job == self.fails:
            raise self.raises
        if job in self.busy:
            trace.emit("paypal", f"{self.name} {job}")
        record = trace._current.get()
        if job in self.visitor and record is not None:
            record.add("demo", "visitor's own step", trace.OK, None, {})

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


@pytest.mark.edge
async def test_a_runs_card_leaves_out_timed_work_that_ran_during_it() -> None:
    registry, [visitor] = _workspaces(True, _Gateway("a", busy=("reconcile_if_due",)))
    record = registry.trace(visitor)
    token = trace.use(record)
    try:
        begun = run_report.begin("demo", "Scenario started")
        await _tend_all(registry)  # the first reconciliation lands mid-run
        trace.emit("ledger", "Record entry #2: attack_caught")
        ending = run_report.Ending(None, caught=True)
        run_report.finish(begun, ending, run_report.AUTOMATICALLY)
    finally:
        trace.reset(token)
    card = record.after(0)[-1]  # type: ignore[union-attr]
    assert card.layer == "outcome"
    assert (card.detail["record_entries"], card.detail["paypal_calls"]) == ("1", "0")
    assert card.detail["steps"] == "2"


@pytest.mark.edge
def test_the_attack_line_holds_with_or_without_a_proposal() -> None:
    record = trace.Trace()
    token = trace.use(record)
    try:
        ai_checks._report_screening(ScreeningVerdict.INJECTION, "model")
        ai_checks._report_guard(True, "0.9700")
    finally:
        trace.reset(token)
    for step in record.after(0):
        assert "request" not in step.detail["safe"]
        assert "nothing is sent" in step.detail["safe"]


@pytest.mark.edge
async def test_a_visitors_step_during_a_job_is_not_labelled_automatic() -> None:
    registry, [visitor] = _workspaces(True, _Gateway("a", visitor=("expire",)))
    await _tend_all(registry)
    assert _titles(registry, visitor) == ["visitor's own step"]


@pytest.mark.edge
async def test_an_unexpected_error_in_a_job_stops_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _Gateway("a", fails="settle_pending", raises=KeyError("surprise"))
    second = _Gateway("b")
    registry, _ = _workspaces(True, first, second)
    await _tend_all(registry)
    assert first.ran[-1] == "check_records_if_due"
    assert second.ran[-1] == "check_records_if_due"
    assert "refunds pending at PayPal failed" in caplog.text


@pytest.mark.edge
async def test_a_job_that_always_fails_never_starves_the_later_jobs() -> None:
    gateway = _Gateway("a", fails="expire", raises=ValueError("always"))
    registry, _ = _workspaces(True, gateway)
    await _tend_all(registry)
    await _tend_all(registry)
    assert gateway.ran.count("reconcile_if_due") == 2


@pytest.mark.edge
async def test_shutdown_still_cancels_a_running_job() -> None:
    gateway = _Gateway("a", fails="release_due", raises=asyncio.CancelledError())
    registry, _ = _workspaces(True, gateway)
    with pytest.raises(asyncio.CancelledError):
        await _tend_all(registry)
    assert "check_records_if_due" not in gateway.ran
    assert not trace.active()
