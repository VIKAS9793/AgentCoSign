"""The live step record behind the technical view on the hosted demo.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Nothing being traced (outside the demo) | A reported step goes nowhere |
| Two workspaces traced in turn | Each keeps only its own steps |
| More steps than the limit | The oldest go; numbering keeps rising |
| A reader resumes after the last step it saw | Gets only newer steps |
| A timed step that fails | Recorded as failed, and the error still raised |
| Work handed to a thread | Its steps land in the same workspace |
"""

import asyncio

import pytest

from agentcosign.core import trace


def test_outside_a_traced_request_nothing_is_kept() -> None:
    token = trace.use(None)
    try:
        trace.emit("paypal", "Order read")
    finally:
        trace._current.reset(token)


@pytest.mark.edge
def test_each_workspace_keeps_only_its_own_steps() -> None:
    first, second = trace.Trace(), trace.Trace()
    token = trace.use(first)
    trace.emit("ai", "Screening", verdict="clean")
    trace._current.reset(token)
    token = trace.use(second)
    trace.emit("paypal", "Refund sent")
    trace._current.reset(token)
    assert [s.title for s in first.after(0)] == ["Screening"]
    assert [s.title for s in second.after(0)] == ["Refund sent"]


@pytest.mark.edge
def test_the_oldest_steps_go_past_the_limit_and_numbering_rises() -> None:
    record = trace.Trace(limit=3)
    token = trace.use(record)
    for n in range(5):
        trace.emit("rules", f"step {n}")
    trace._current.reset(token)
    steps = record.after(0)
    assert [s.title for s in steps] == ["step 2", "step 3", "step 4"]
    assert [s.seq for s in steps] == [3, 4, 5]
    assert [s.title for s in record.after(4)] == ["step 4"]


@pytest.mark.edge
def test_a_failed_timed_step_is_recorded_and_raised() -> None:
    record = trace.Trace()
    token = trace.use(record)
    with pytest.raises(ValueError), trace.timed("paypal", "Create order"):
        raise ValueError("declined")
    trace._current.reset(token)
    (step,) = record.after(0)
    assert step.status == trace.FAIL
    assert step.ms is not None


@pytest.mark.edge
async def test_steps_from_a_worker_thread_land_in_the_workspace() -> None:
    record = trace.Trace()
    token = trace.use(record)
    try:
        await asyncio.to_thread(trace.emit, "ledger", "Entry appended", seq=7)
    finally:
        trace._current.reset(token)
    (step,) = record.after(0)
    assert step.detail == {"seq": "7"}
