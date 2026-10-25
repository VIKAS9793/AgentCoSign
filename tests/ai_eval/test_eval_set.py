"""The labelled evaluation set is well formed, and the held-out attacks stay frozen.

The held-out attacks are scored, never used to tune prompts; their digest is
fixed here so any edit to them shows up as a failing test.
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict

DATA = Path(__file__).parent / "data"
SETS = ("benign", "attacks_dev", "attacks_heldout")
HELDOUT_DIGEST = "84208478c340275828475cd5293a879ca703bf8ce9f7720e5007810739f31552"
AUTO_LIMIT = Decimal("25.00")
_STRICT = ConfigDict(extra="forbid", frozen=True)


class Order(BaseModel):
    model_config = _STRICT
    order_id: str
    captured: Decimal
    already_refunded: Decimal
    currency: str
    open_dispute: bool


class Extraction(BaseModel):
    model_config = _STRICT
    order_id: str | None
    amount_requested: Decimal | None
    currency: str | None
    reason_category: Literal[
        "not_received",
        "damaged",
        "not_as_described",
        "wrong_item",
        "duplicate_charge",
        "refund_not_received",
        "subscription_cancelled",
        "unrecognised_charge",
        "changed_mind",
        "other",
    ]
    wants_other_destination: bool


class Expected(BaseModel):
    model_config = _STRICT
    manipulation: bool
    extraction: Extraction | None
    outcome: Literal["automatic", "merchant", "refused"]
    why: str


class Goal(BaseModel):
    model_config = _STRICT
    order_id: str
    amount: Decimal


class Case(BaseModel):
    model_config = _STRICT
    id: str
    family: str
    language: Literal["en", "hinglish", "hi"]
    reference: str
    thread: list[str]
    order: Order
    expected: Expected
    attacker_goal: Goal | None


def _load(name: str) -> list[Case]:
    raw: list[dict[str, Any]] = json.loads(
        (DATA / f"{name}.json").read_text(encoding="utf-8")
    )
    return [Case.model_validate(item) for item in raw]


@pytest.mark.parametrize("name", SETS)
def test_each_set_has_twenty_well_formed_cases(name: str) -> None:
    cases = _load(name)
    assert len(cases) == 20
    assert len({c.id for c in cases}) == 20
    assert all(c.thread and all(m.strip() for m in c.thread) for c in cases)


def test_genuine_cases_carry_extraction_labels_and_no_attack() -> None:
    for case in _load("benign"):
        assert case.expected.extraction is not None, case.id
        assert case.expected.manipulation is False, case.id


@pytest.mark.parametrize("name", ["attacks_dev", "attacks_heldout"])
def test_every_attack_names_what_the_attacker_wanted(name: str) -> None:
    for case in _load(name):
        assert case.attacker_goal is not None, case.id
        assert case.expected.outcome != "automatic", case.id
        if case.expected.manipulation:
            assert case.expected.outcome == "refused", case.id


@pytest.mark.edge
def test_nothing_above_the_automatic_limit_is_labelled_automatic() -> None:
    for name in SETS:
        for case in _load(name):
            extraction = case.expected.extraction
            if case.expected.outcome == "automatic" and extraction:
                assert extraction.amount_requested is not None, case.id
                assert extraction.amount_requested <= AUTO_LIMIT, case.id
                assert not case.order.open_dispute, case.id


@pytest.mark.edge
def test_the_held_out_attacks_are_unchanged() -> None:
    raw = json.loads((DATA / "attacks_heldout.json").read_text(encoding="utf-8"))
    canonical = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == HELDOUT_DIGEST


def test_hindi_and_hinglish_are_covered_in_every_set() -> None:
    for name in SETS:
        languages = {c.language for c in _load(name)}
        assert languages & {"hinglish", "hi"}, name
