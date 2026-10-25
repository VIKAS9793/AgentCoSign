"""Remove card, identity and contact details from a customer's message before
anything keeps it or a model reads it.

Masking is one way: nothing downstream needs the real value, and a value never
kept cannot leak. It leans towards masking, because a wrongly masked tracking
number costs a question to the merchant while a kept card number puts the whole
shop in card-security scope. Names are not masked: no fixed rule finds them
reliably. Order ids, amounts and dates are never touched, since the reading of
the customer's words needs them.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_EDGE_L = r"(?<![\w+@.-])"
_EDGE_R = r"(?![\w@])"
_CODE_WORDS = (
    r"cvv2?|cvc2?|cid|security\s+code|card\s+code|pin|otp|passcode|"
    r"one[\s-]time\s+(?:password|code)"
)
_SECURITY_CODE = re.compile(
    rf"(?i)\b({_CODE_WORDS})\b([^\d\n]{{0,15}}?)(\d{{3,8}})(?!\d)"
)
_CARD = re.compile(rf"{_EDGE_L}(?:\d[ -]?){{12,18}}\d{_EDGE_R}")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_UPI = re.compile(r"(?<![\w.-])[\w.-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}(?![\w.@])")
_IBAN = re.compile(rf"{_EDGE_L}[A-Z]{{2}}\d{{2}}(?: ?[A-Z0-9]){{11,30}}{_EDGE_R}")
_PAN = re.compile(rf"(?i){_EDGE_L}[A-Z]{{5}}\d{{4}}[A-Z]{_EDGE_R}")
_AADHAAR = re.compile(rf"{_EDGE_L}[2-9]\d{{3}}([ -]?)\d{{4}}\1\d{{4}}{_EDGE_R}")
_PHONE = re.compile(
    rf"{_EDGE_L}(?:\+\d{{1,3}}[ -]?)?(?:\(\d{{2,5}}\)|\d{{2,5}})"
    rf"(?:[ -]?(?:\(\d{{2,5}}\)|\d{{2,5}})){{1,4}}{_EDGE_R}"
)
_LABEL = re.compile(r"\[(?:card ending \d{4}|[A-Za-z ]+ removed)\]")
# phone numbers run from 10 digits (a local mobile) to 15 (the longest
# international number)
_PHONE_DIGITS = range(10, 16)
# no country's bank account number in this format is 17 characters long, and
# 17 is exactly a PayPal order id, so that length is never masked as one
_ORDER_ID_LENGTH = 17


@dataclass(frozen=True, slots=True)
class MaskedText:
    """The masked message and how many of each kind were removed (no values)."""

    text: str
    counts: Mapping[str, int]


def _normalise(text: str) -> str:
    """Fold look-alike digits to plain ones and drop invisible characters, so a
    disguised number is still recognised."""
    folded = unicodedata.normalize("NFKC", text)
    out = []
    for ch in folded:
        if unicodedata.category(ch) == "Cf":
            continue
        digit = unicodedata.decimal(ch, None)
        out.append(str(digit) if digit is not None and not ch.isascii() else ch)
    return "".join(out)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _passes_card_check(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _passes_iban_check(value: str) -> bool:
    compact = value.replace(" ", "")
    if len(compact) == _ORDER_ID_LENGTH:
        return False
    moved = compact[4:] + compact[:4]
    number = "".join(str(int(ch, 36)) for ch in moved)
    return int(number) % 97 == 1


Replacer = Callable[[re.Match[str]], str | None]


def _card(match: re.Match[str]) -> str | None:
    digits = _digits(match.group(0))
    return f"[card ending {digits[-4:]}]" if _passes_card_check(digits) else None


def _phone(match: re.Match[str]) -> str | None:
    return "[phone removed]" if len(_digits(match.group(0))) in _PHONE_DIGITS else None


def _iban(match: re.Match[str]) -> str | None:
    return "[bank account removed]" if _passes_iban_check(match.group(0)) else None


def _fixed(label: str) -> Replacer:
    return lambda _match: label


_STEPS: tuple[tuple[str, re.Pattern[str], Replacer], ...] = (
    ("card", _CARD, _card),
    ("email", _EMAIL, _fixed("[email removed]")),
    ("upi", _UPI, _fixed("[UPI id removed]")),
    ("iban", _IBAN, _iban),
    ("pan", _PAN, _fixed("[PAN removed]")),
    ("aadhaar", _AADHAAR, _fixed("[Aadhaar removed]")),
    ("phone", _PHONE, _phone),
)


def _apply(
    text: str,
    kind: str,
    pattern: re.Pattern[str],
    replace: Replacer,
    counts: Counter[str],
) -> str:
    def swap(match: re.Match[str]) -> str:
        """The label for a real match; anything else stays as written."""
        label = replace(match)
        if label is None:
            return match.group(0)
        counts[kind] += 1
        return label

    return pattern.sub(swap, text)


def _cap(text: str, limit: int) -> str:
    """Cut to the limit, stepping back rather than cutting through a mask."""
    if len(text) <= limit:
        return text
    for label in _LABEL.finditer(text):
        if label.start() < limit < label.end():
            return text[: label.start()]
    return text[:limit]


def mask(text: str, limit: int) -> MaskedText:
    """The message with sensitive details replaced, capped at ``limit`` chars."""
    counts: Counter[str] = Counter()
    masked = _normalise(text)

    def code(match: re.Match[str]) -> str:
        """Keep the word that names the code; drop the code."""
        counts["security_code"] += 1
        return f"{match.group(1)}{match.group(2)}[security code removed]"

    masked = _SECURITY_CODE.sub(code, masked)
    for kind, pattern, replace in _STEPS:
        masked = _apply(masked, kind, pattern, replace, counts)
    return MaskedText(_cap(masked, limit), dict(counts))
