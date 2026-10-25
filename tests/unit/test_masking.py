"""Customer messages lose card, identity and contact details before anything
keeps them or a model reads them.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Card number: plain, spaced, dashed; 13, 15, 16, 19 digits | Last four kept |
| 16 digits failing the card checksum (an order or tracking number) | Kept |
| Fullwidth or Devanagari digits forming a card number | Masked |
| Zero-width characters hidden between card digits | Masked |
| Digits after cvv, cvc, security code, OTP, PIN | Masked, word kept |
| Email address; UPI id (`name@okbank`) | Masked |
| Indian mobile with and without +91; international number | Masked |
| Aadhaar, grouped and contiguous | Masked |
| PAN, upper and lower case | Masked |
| IBAN (valid checksum), spaced and compact | Masked |
| IBAN shape failing its checksum | Not called a bank account |
| PayPal order id (17 letters and digits) | Kept, never mistaken for anything |
| Amounts ($24.99, 24,99 EUR, Rs 2,499, 1000.00) and dates | Kept |
| Text over the cap | Cut to the cap, never through a mask |
| Nothing to mask | Unchanged, nothing counted |
| Counts | One count per kind, no values |
"""

import pytest

from agentcosign.services.masking import mask

ORDER_ID = "5RT88213KL0099341"


def _with_check_digit(digits: str) -> str:
    """Append the card-number checksum digit to a run of digits."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return digits + str((10 - total % 10) % 10)


CARD_16 = "4111111111111111"


CARD_FORMS = [
    CARD_16,
    "4111 1111 1111 1111",
    "4111-1111-1111-1111",
    "378282246310005",  # 15 digits
    "4222222222222",  # 13 digits
    _with_check_digit("601100000000000000"),  # 19 digits
]


@pytest.mark.edge
@pytest.mark.parametrize("written", CARD_FORMS)
def test_a_card_number_keeps_only_its_last_four(written: str) -> None:
    result = mask(f"my card {written} was charged", limit=8000)
    last_four = written.replace(" ", "").replace("-", "")[-4:]
    assert result.text == f"my card [card ending {last_four}] was charged"
    assert result.counts == {"card": 1}


@pytest.mark.edge
def test_a_long_number_failing_the_card_checksum_is_kept() -> None:
    tracking = "4111111111111112"
    assert mask(f"tracking {tracking}", limit=8000).text == f"tracking {tracking}"


DISGUISED_CARDS = [
    "４１１１" + "1111" + "1111" + "1111",  # fullwidth digits
    "".join(chr(0x0966 + int(d)) for d in CARD_16),  # Devanagari digits
    "4111​1111​1111​1111",  # zero-width spaces
]


@pytest.mark.edge
@pytest.mark.parametrize("written", DISGUISED_CARDS)
def test_a_disguised_card_number_is_still_masked(written: str) -> None:
    result = mask(f"card {written}", limit=8000)
    assert result.text == "card [card ending 1111]"


@pytest.mark.edge
@pytest.mark.parametrize(
    ("written", "keyword"),
    [
        ("CVV: 123", "CVV"),
        ("cvc 1234", "cvc"),
        ("security code is 987", "security code"),
        ("OTP 482913", "OTP"),
        ("my pin is 4321", "pin"),
    ],
)
def test_security_codes_are_removed_but_the_keyword_stays(
    written: str, keyword: str
) -> None:
    result = mask(written, limit=8000)
    assert keyword in result.text
    assert "[security code removed]" in result.text
    assert not any(ch.isdigit() for ch in result.text)


@pytest.mark.edge
@pytest.mark.parametrize(
    ("written", "label", "kind"),
    [
        ("mail me at jane.doe+shop@example.co.uk", "[email removed]", "email"),
        ("pay to jane.doe@okaxis", "[UPI id removed]", "upi"),
        ("call 9876543210", "[phone removed]", "phone"),
        ("call +91 98765 43210", "[phone removed]", "phone"),
        ("call +1 (415) 555-0123", "[phone removed]", "phone"),
        ("aadhaar 2345 6789 0123", "[Aadhaar removed]", "aadhaar"),
        ("aadhaar 234567890123", "[Aadhaar removed]", "aadhaar"),
        ("PAN ABCDE1234F", "[PAN removed]", "pan"),
        ("pan abcde1234f", "[PAN removed]", "pan"),
        ("IBAN GB82 WEST 1234 5698 7654 32", "[bank account removed]", "iban"),
        ("IBAN DE89370400440532013000", "[bank account removed]", "iban"),
    ],
)
def test_identity_and_contact_details_are_masked(
    written: str, label: str, kind: str
) -> None:
    result = mask(written, limit=8000)
    assert label in result.text
    assert result.counts == {kind: 1}


@pytest.mark.edge
def test_an_iban_shape_failing_its_checksum_is_not_called_a_bank_account() -> None:
    """Masking leans towards hiding: the digits may still go as a phone-length
    run, but the text is never labelled a bank account it is not."""
    result = mask("ref GB00 WEST 1234 5698 7654 32", limit=8000)
    assert "iban" not in result.counts
    assert result.text.startswith("ref GB00 WEST ")


UNTOUCHED = [
    f"order {ORDER_ID} never arrived",
    "order 9AB12345CD6789012, refund $24.99 please",
    "order 7XY00000000000001 cost 24,99 EUR",
    "I paid Rs 2,499 on 2026-10-25 at 14:30",
    "refund 1000.00 USD for order 25-10-2026",
]


@pytest.mark.edge
@pytest.mark.parametrize("text", UNTOUCHED)
def test_order_ids_amounts_and_dates_are_never_masked(text: str) -> None:
    result = mask(text, limit=8000)
    assert result.text == text
    assert result.counts == {}


@pytest.mark.edge
def test_the_cap_never_cuts_through_a_mask() -> None:
    text = "x" * 20 + f" {CARD_16}"
    masked = mask(text, limit=8000).text
    cut = mask(text, limit=len(masked) - 3).text
    assert cut == "x" * 20 + " "
    assert mask("abcdef", limit=4).text == "abcd"


@pytest.mark.edge
def test_counts_name_each_kind_without_values() -> None:
    result = mask(f"{CARD_16} and 378282246310005, mail a@b.com, cvv 123", limit=8000)
    assert result.counts == {"card": 2, "email": 1, "security_code": 1}
    assert "4111" not in str(result.counts)
