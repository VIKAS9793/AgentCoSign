"""The live update stream: change hints only, coalesced, closed on sign-out."""

import pytest

from agentcosign.services.live_updates import change_events


class Clock:
    """Hands out versions and session validity one check at a time."""

    def __init__(self, versions: list[int], valid: list[bool]) -> None:
        self.versions = versions
        self.valid = valid
        self.checks = 0

    async def version(self) -> int:
        return self.versions[min(self.checks, len(self.versions) - 1)]

    async def session_ok(self) -> bool:
        return self.valid[min(self.checks, len(self.valid) - 1)]

    async def wait(self, _seconds: int) -> None:
        self.checks += 1


async def _collect(clock: Clock, limit: int, keepalive_every: int = 100) -> list[str]:
    out: list[str] = []
    async for chunk in change_events(
        clock.version, clock.session_ok, clock.wait, keepalive_every=keepalive_every
    ):
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


@pytest.mark.edge
async def test_a_change_sends_one_hint_with_no_money_details() -> None:
    clock = Clock([1, 1, 2, 2], [True])
    events = await _collect(clock, 2)
    assert events[0] == 'event: changed\ndata: {"version":1}\n\n'  # first sync
    assert events[1] == 'event: changed\ndata: {"version":2}\n\n'
    assert all("$" not in e and "amount" not in e for e in events)


@pytest.mark.edge
async def test_many_changes_between_checks_become_one_hint() -> None:
    clock = Clock([1, 7, 7], [True])
    events = await _collect(clock, 2)
    assert events[1] == 'event: changed\ndata: {"version":7}\n\n'


@pytest.mark.edge
async def test_the_stream_says_signed_out_and_stops_when_the_session_ends() -> None:
    clock = Clock([1], [True, True, False])
    events = await _collect(clock, 10)
    assert events[-1] == "event: signed_out\ndata: {}\n\n"
    assert len(events) == 2


async def test_a_quiet_stream_sends_keep_alives() -> None:
    clock = Clock([1], [True])
    events = await _collect(clock, 3, keepalive_every=1)
    assert events[1] == ": keep-alive\n\n"
