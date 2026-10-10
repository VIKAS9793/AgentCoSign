"""Stable, privacy-preserving keys for customers and payees.

Card orders from PayPal carry no payer id, only the card brand, last four digits
and cardholder name. Those three together identify a paying customer across
orders well enough for a velocity limit; a rare collision only causes an extra
escalation, never a wrong payment. The values are combined with a secret key so
the stored digest cannot be reversed or matched against a list of names.
"""

import hashlib
import hmac
import unicodedata

KEY_PREFIX_CARD = "card:"
KEY_PREFIX_PAYEE = "payee:"
KEY_PREFIX_PAYER = "payer:"
DIGEST_CHARS = 32


def _normalise(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(folded.split())


def keyed_digest(secret: bytes, value: str) -> str:
    """Hex digest of a normalised value under a secret key (truncated)."""
    mac = hmac.new(secret, _normalise(value).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:DIGEST_CHARS]


def card_fingerprint(
    secret: bytes, brand: str, last_four: str, name: str
) -> str | None:
    """Customer key for a card payment, or None when the parts are incomplete."""
    parts = [brand.strip(), last_four.strip(), name.strip()]
    if not all(parts) or not (len(parts[1]) == 4 and parts[1].isdigit()):
        return None
    return KEY_PREFIX_CARD + keyed_digest(secret, "|".join(parts))


def payee_key(secret: bytes, payee_id: str) -> str:
    """Key for a payout recipient, so payee details are not stored in clear."""
    return KEY_PREFIX_PAYEE + keyed_digest(secret, payee_id)


def payer_key(secret: bytes, payer_id: str) -> str:
    """Customer key for a PayPal wallet payment, from PayPal's payer id."""
    return KEY_PREFIX_PAYER + keyed_digest(secret, payer_id)
