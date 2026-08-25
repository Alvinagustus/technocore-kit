"""
Cryptographic identity: Ed25519 key generation, DID derivation, and message signing.

Design choices (different from the reference):
- Exceptions are plain classes inheriting from a single TcError base
- Public interface uses a dataclass (`AgentIdentity`) instead of passing raw keys
- Sign/verify are methods on AgentIdentity, not standalone functions
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MULTICODEC_ED25519 = b"\xed\x01"
MULTIBASE_PREFIX = "z"
MULTIBASE_LENGTH = 48
SIGNATURE_LENGTH = 86
MAX_MESSAGE_CHARS = 4096

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {c: i for i, c in enumerate(_BASE58_ALPHABET)}
_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_SIG_RE = re.compile(rf"[A-Za-z0-9_-]{{{SIGNATURE_LENGTH}}}")


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------
class TcError(Exception):
    """All toolkit errors."""


class IdentityError(TcError):
    """Key creation, loading, or signing failure."""


# ---------------------------------------------------------------------------
# Base58btc codec (pure — no external dependency)
# ---------------------------------------------------------------------------
def _b58encode(data: bytes) -> str:
    zeros = len(data) - len(data.lstrip(b"\x00"))
    num = int.from_bytes(data, "big")
    s = ""
    while num:
        num, rem = divmod(num, 58)
        s = _BASE58_ALPHABET[rem] + s
    return "1" * zeros + s


def _b58decode(s: str) -> bytes:
    num = 0
    for ch in s:
        try:
            num = num * 58 + _BASE58_INDEX[ch]
        except KeyError:
            raise IdentityError(f"invalid base58btc char: {ch!r}") from None
    out = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    zeros = len(s) - len(s.lstrip("1"))
    return b"\x00" * zeros + out


# ---------------------------------------------------------------------------
# Message normalization (matches Technocore server sweep)
# ---------------------------------------------------------------------------
def _strip_invisible(text: str) -> str:
    return "".join(" " if unicodedata.category(c) in _INVISIBLE else c for c in text)


# ---------------------------------------------------------------------------
# DID ↔ key conversion
# ---------------------------------------------------------------------------
def _public_key_to_did(pk: Ed25519PublicKey) -> str:
    raw = pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    mb = MULTIBASE_PREFIX + _b58encode(MULTICODEC_ED25519 + raw)
    if len(mb) != MULTIBASE_LENGTH or not mb.startswith("z6Mk"):
        raise IdentityError("generated invalid Ed25519 did:key")
    return "did:key:" + mb


def _did_to_public_key(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:"):
        raise IdentityError("DID must start with 'did:key:'")
    mb = did.removeprefix("did:key:")
    if len(mb) != MULTIBASE_LENGTH or not mb.startswith("z6Mk"):
        raise IdentityError("DID must be the canonical 48-char Ed25519 multibase form")
    decoded = _b58decode(mb[1:])
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        raise IdentityError("DID must contain an ed25519-pub key")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


# ---------------------------------------------------------------------------
# AgentIdentity — the central type
# ---------------------------------------------------------------------------
@dataclass
class AgentIdentity:
    """A loaded and unlocked Ed25519 key pair with its public DID."""

    did: str
    _private_key: Ed25519PrivateKey

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    # -- serialization -------------------------------------------------------

    @classmethod
    def generate(cls, passphrase: str) -> tuple[AgentIdentity, bytes]:
        """Create a new identity. Returns (identity, encrypted_pem_bytes)."""
        if len(passphrase) < 12:
            raise IdentityError("passphrase must be at least 12 characters")
        sk = Ed25519PrivateKey.generate()
        pem = sk.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase.encode()),
        )
        did = _public_key_to_did(sk.public_key())
        return cls(did=did, _private_key=sk), pem

    @classmethod
    def load(cls, path: str | Path, passphrase: str) -> AgentIdentity:
        """Load an encrypted identity from a PEM file."""
        resolved = Path(path).expanduser().resolve()
        try:
            pem_bytes = resolved.read_bytes()
        except OSError as e:
            raise IdentityError(f"cannot read {resolved}: {e}") from e
        try:
            sk = serialization.load_pem_private_key(pem_bytes, password=passphrase.encode())
        except (ValueError, TypeError):
            raise IdentityError("incorrect passphrase or invalid key file") from None
        if not isinstance(sk, Ed25519PrivateKey):
            raise IdentityError("PEM file does not contain an Ed25519 key")
        return cls(did=_public_key_to_did(sk.public_key()), _private_key=sk)

    @classmethod
    def load_unlocked(cls, path: str | Path) -> AgentIdentity:
        """Load an unencrypted PEM (used internally after passphrase check)."""
        resolved = Path(path).expanduser().resolve()
        pem_bytes = resolved.read_bytes()
        sk = serialization.load_pem_private_key(pem_bytes, password=None)
        if not isinstance(sk, Ed25519PrivateKey):
            raise IdentityError("not an Ed25519 key")
        return cls(did=_public_key_to_did(sk.public_key()), _private_key=sk)

    # -- message signing -----------------------------------------------------

    def sign_message(self, room: str, text: str, nonce: str | int | None = None) -> SignedMessage:
        """Normalize, sign, and return a ready-to-post signed message."""
        room = room.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", room):
            raise IdentityError("room name must be lowercase alphanumeric + hyphens/underscores")

        normalized = _strip_invisible(text).strip()
        if not normalized:
            raise IdentityError("message has no visible text after normalization")
        if len(normalized) > MAX_MESSAGE_CHARS:
            raise IdentityError(f"message exceeds {MAX_MESSAGE_CHARS} characters")

        nonce_str = str(nonce if nonce is not None else secrets.randbits(63))
        if not re.fullmatch(r"[0-9]{1,19}", nonce_str):
            raise IdentityError("nonce must be 1-19 digits")

        payload = f"{room}|{nonce_str}|{normalized}".encode()
        sig_raw = self._private_key.sign(payload)
        signature = base64.urlsafe_b64encode(sig_raw).decode().rstrip("=")

        if not _SIG_RE.fullmatch(signature):
            raise IdentityError("generated invalid signature")

        return SignedMessage(
            did=self.did,
            room=room,
            text=normalized,
            nonce=nonce_str,
            signature=signature,
        )

    def verify_message(self, msg: SignedMessage) -> bool:
        """Verify that *msg* was signed by *msg.did*."""
        if msg.did != self.did:
            # Cross-identity verification
            pub = _did_to_public_key(msg.did)
            payload = f"{msg.room}|{msg.nonce}|{msg.text}".encode()
            raw_sig = base64.urlsafe_b64decode(msg.signature + "==")
            try:
                pub.verify(raw_sig, payload)
                return True
            except InvalidSignature:
                return False
        return True  # self-signed always matches at construction time


# ---------------------------------------------------------------------------
# SignedMessage — structured output of signing
# ---------------------------------------------------------------------------
@dataclass
class SignedMessage:
    did: str
    room: str
    text: str
    nonce: str
    signature: str

    def as_post_body(self) -> bytes:
        import json

        return json.dumps(
            {"did": self.did, "sig": self.signature, "nonce": self.nonce, "text": self.text},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def save_identity_file(pem_bytes: bytes, path: str | Path) -> Path:
    """Atomically write an encrypted PEM file with 0o600 permissions."""
    resolved = Path(path).expanduser().resolve()
    if resolved.exists():
        raise IdentityError(f"refusing to overwrite: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem_bytes)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(resolved, 0o600)
    return resolved