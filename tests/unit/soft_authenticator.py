"""A software passkey (WebAuthn authenticator) for tests.

Builds real registration and assertion responses with an ECDSA P-256 key, so the
server-side verification runs exactly as it would against a phone.
"""

import hashlib
import json
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

FLAG_USER_PRESENT = 0x01
FLAG_USER_VERIFIED = 0x04
FLAG_ATTESTED_DATA = 0x40


class SoftAuthenticator:
    """One passkey on one simulated device."""

    def __init__(
        self, rp_id: str, origin: str, credential_id: bytes = b"cred-1"
    ) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.credential_id = credential_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.sign_count = 0

    @property
    def id(self) -> str:
        return bytes_to_base64url(self.credential_id)

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # key type: elliptic curve
                3: -7,  # algorithm: ES256
                -1: 1,  # curve: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, challenge: bytes, origin: str | None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": bytes_to_base64url(challenge),
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()

    def register(self, options_json: str, verified: bool = True) -> dict[str, Any]:
        """Answer registration options the way a browser would."""
        options = json.loads(options_json)
        challenge = base64url_to_bytes(options["challenge"])
        flags = FLAG_USER_PRESENT | FLAG_ATTESTED_DATA
        if verified:
            flags |= FLAG_USER_VERIFIED
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([flags])
            + struct.pack(">I", self.sign_count)
            + b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", challenge, None)
                ),
                "attestationObject": bytes_to_base64url(attestation),
            },
        }

    def sign(
        self,
        options_json: str,
        *,
        verified: bool = True,
        origin: str | None = None,
        challenge_override: bytes | None = None,
    ) -> dict[str, Any]:
        """Answer authentication options the way a browser would."""
        options = json.loads(options_json)
        challenge = challenge_override or base64url_to_bytes(options["challenge"])
        self.sign_count += 1
        flags = FLAG_USER_PRESENT | (FLAG_USER_VERIFIED if verified else 0)
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + bytes([flags])
            + struct.pack(">I", self.sign_count)
        )
        client_data = self._client_data("webauthn.get", challenge, origin)
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }
