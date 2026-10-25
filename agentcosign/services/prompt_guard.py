"""The prompt guard: a small classifier from a different model family than the
assistant, scoring how much a customer's message reads like an attempt to
instruct the assistant ("ignore all previous instructions").

It catches open attack wording well and misses social engineering (a message
claiming to be the shop or PayPal), which the screening model reads for, so
neither replaces the other. Its window is short: a long message is read in
overlapping pieces and the worst piece counts. A score it cannot give, for any
piece, means the check could not run, never that the message is clean.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from agentcosign.clients.llm import LLMProvider
from agentcosign.core.exceptions import ModelUnavailableError

# the classifier reads about 512 tokens; this many characters stays inside that
PIECE_CHARS = 1500
# an attack phrase cut by a piece boundary is still read whole in the overlap
PIECE_OVERLAP = 200
_NO_SCORE = Decimal(0)


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The worst score over the message, whether it reached the threshold, and
    which model gave it (None when nothing needed reading)."""

    score: Decimal
    flagged: bool
    model: str | None


def pieces(text: str) -> list[str]:
    """Overlapping pieces that each fit the classifier's window."""
    step = PIECE_CHARS - PIECE_OVERLAP
    ends = range(0, max(len(text) - PIECE_OVERLAP, 1), step)
    return [text[start : start + PIECE_CHARS] for start in ends]


def _probability(answer: str) -> Decimal:
    try:
        score = Decimal(answer.strip())
    except InvalidOperation as exc:
        raise ModelUnavailableError("the guard gave no score") from exc
    if not score.is_finite() or not Decimal(0) <= score <= Decimal(1):
        raise ModelUnavailableError("the guard gave no usable score")
    return score


async def guard(provider: LLMProvider, text: str, threshold: Decimal) -> GuardResult:
    """Score a customer's message; flagged at or above the threshold."""
    if not text.strip():
        return GuardResult(_NO_SCORE, False, None)
    worst, model = _NO_SCORE, None
    for piece in pieces(text):
        answer = await provider.complete([{"role": "user", "content": piece}])
        worst = max(worst, _probability(answer.text))
        model = answer.model
    return GuardResult(worst, worst >= threshold, model)
