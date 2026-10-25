"""Private workspaces for the hosted demo: each visitor gets their own gateway.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Not the demo | No workspaces open; only the owner's workspace exists |
| Two visitors open workspaces | Different ids and different gateways |
| Unknown, malformed or forged id | Nothing found |
| One address opens more than its daily allowance | Refused; others still can |
| A day after the first setups | The address may open again |
| More live workspaces than allowed | The least recently used one closes first |
| A workspace used recently | Kept over an idle one |
| Background work | Covers the owner's workspace and every live one |
"""

from collections.abc import Callable

import pytest

from agentcosign.core.config import DemoSettings
from agentcosign.core.exceptions import LimitReachedError
from agentcosign.services.workspaces import WORKSPACE_ID, Workspaces

LIMITS = DemoSettings(
    max_workspaces=3,
    setups_per_address_per_day=2,
    scenarios_per_workspace_per_day=15,
    message_max_chars=500,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _registry(
    demo: bool = True, clock: Callable[[], float] | None = None
) -> Workspaces[str]:
    return Workspaces(
        default="owner",
        build=lambda workspace_id: f"gateway:{workspace_id}",
        demo=demo,
        limits=LIMITS,
        clock=clock or _Clock(),
    )


@pytest.mark.edge
def test_outside_the_demo_no_workspace_can_be_opened() -> None:
    registry = _registry(demo=False)
    with pytest.raises(LimitReachedError):
        registry.open("1.2.3.4")
    assert registry.live() == ["owner"]


def test_two_visitors_get_separate_workspaces() -> None:
    registry = _registry()
    first, second = registry.open("1.1.1.1"), registry.open("2.2.2.2")
    assert first != second
    assert WORKSPACE_ID.fullmatch(first) and WORKSPACE_ID.fullmatch(second)
    assert registry.get(first) == f"gateway:{first}"
    assert registry.get(second) == f"gateway:{second}"


# unknown, malformed, and a path trying to leave the folder
FOREIGN_IDS = ["ws-" + "0" * 32, "ws-123", "../ledger", ""]


@pytest.mark.edge
@pytest.mark.parametrize("workspace_id", FOREIGN_IDS)
def test_an_id_that_was_never_opened_finds_nothing(workspace_id: str) -> None:
    registry = _registry()
    registry.open("1.1.1.1")
    assert registry.get(workspace_id) is None


@pytest.mark.edge
def test_one_address_is_limited_per_day_and_others_are_not() -> None:
    clock = _Clock()
    registry = _registry(clock=clock)
    registry.open("1.1.1.1")
    registry.open("1.1.1.1")
    with pytest.raises(LimitReachedError):
        registry.open("1.1.1.1")
    registry.open("2.2.2.2")
    clock.now += 24 * 3600 + 1
    registry.open("1.1.1.1")


@pytest.mark.edge
def test_the_least_recently_used_workspace_closes_first() -> None:
    clock = _Clock()
    registry = _registry(clock=clock)
    first = registry.open("1.1.1.1")
    second = registry.open("2.2.2.2")
    third = registry.open("3.3.3.3")
    registry.get(first)  # used again, so the second is now the oldest
    fourth = registry.open("4.4.4.4")
    assert registry.get(second) is None
    assert registry.get(first) is not None
    assert registry.get(third) is not None
    assert registry.get(fourth) is not None


def test_background_work_covers_every_live_workspace() -> None:
    registry = _registry()
    opened = [registry.open(f"{n}.0.0.0") for n in range(2)]
    assert registry.live() == ["owner"] + [f"gateway:{w}" for w in opened]
