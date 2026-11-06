"""GitHub App authentication on the standard library: sign a JWT with the App's private key (RS256), then trade
it for an installation token.

GitHub App keys are RSA keys in PEM form (PKCS#1 "RSA PRIVATE KEY", or PKCS#8 "PRIVATE KEY"). RS256 is RSASSA
PKCS#1 v1.5 with SHA-256, which needs only a DER reader, a hash and modular exponentiation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass

SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")  # DigestInfo for SHA-256


@dataclass
class RSAKey:
    n: int
    e: int
    d: int

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8


def _tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    """One DER element at `pos`: (tag, contents, next position)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    return tag, data[pos : pos + length], pos + length


def _sequence(data: bytes) -> list[tuple[int, bytes]]:
    tag, body, _ = _tlv(data, 0)
    if tag != 0x30:
        raise ValueError("not a DER sequence")
    out, pos = [], 0
    while pos < len(body):
        t, v, pos = _tlv(body, pos)
        out.append((t, v))
    return out


def load_private_key(pem: str) -> RSAKey:
    m = re.search(r"-----BEGIN ((?:RSA )?PRIVATE KEY)-----(.*?)-----END \1-----", pem, re.S)
    if not m:
        raise ValueError("expected a PEM RSA private key")
    der = base64.b64decode("".join(m.group(2).split()))
    if m.group(1) == "PRIVATE KEY":  # PKCS#8 wraps the PKCS#1 key in an OCTET STRING
        parts = _sequence(der)
        der = next(v for t, v in parts if t == 0x04)
    ints = [int.from_bytes(v, "big") for t, v in _sequence(der) if t == 0x02]
    if len(ints) < 4:
        raise ValueError("not an RSA private key")
    return RSAKey(n=ints[1], e=ints[2], d=ints[3])


def sign_rs256(key: RSAKey, message: bytes) -> bytes:
    digest_info = SHA256_PREFIX + hashlib.sha256(message).digest()
    padding = b"\xff" * (key.size - len(digest_info) - 3)
    encoded = b"\x00\x01" + padding + b"\x00" + digest_info
    return pow(int.from_bytes(encoded, "big"), key.d, key.n).to_bytes(key.size, "big")


def verify_rs256(n: int, e: int, message: bytes, signature: bytes) -> bool:
    size = (n.bit_length() + 7) // 8
    encoded = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    return encoded.endswith(SHA256_PREFIX + hashlib.sha256(message).digest()) and encoded.startswith(b"\x00\x01")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key: RSAKey, now: int | None = None) -> str:
    """A JWT for the App itself, valid for 9 minutes (GitHub allows 10), back-dated 60 s for clock drift."""
    t = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64(json.dumps({"iat": t - 60, "exp": t + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    return f"{header}.{claims}.{_b64(sign_rs256(key, signing_input))}"
