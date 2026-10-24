"""Sign a Solana transaction with the bot's own wallet (v5.0.0, live trading).

PumpPortal's Local Transaction API returns an UNSIGNED serialized transaction. This module loads the
bot-only wallet's key and puts its signature in the fee payer's slot, so the key never leaves the
server. It handles both legacy and v0 (versioned) transactions.

Wire format: shortvec(number of signatures) | 64-byte signatures | message. The message starts with
an optional version byte (0x80 | version), then the header (required signatures, read-only signed,
read-only unsigned), shortvec(number of accounts) and 32-byte account keys. Signer i signs the whole
message and its signature goes in slot i.

The key is read once from NORTHSTAR_TRADE_WALLET_PRIVATE_KEY (base58 of the 64-byte secret, as Phantom
and Solflare export it, or the JSON byte array of a solana-keygen file). It is never logged, stored or
shown; only the public address is.
"""
from __future__ import annotations

import json
from typing import Optional

import base58


class KeyError_(ValueError):
    """The private key could not be read (the message never contains the key)."""


def _shortvec_decode(b: bytes, i: int) -> tuple[int, int]:
    n, shift = 0, 0
    while True:
        if i >= len(b):
            raise ValueError("truncated transaction")
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7
        if shift > 14:
            raise ValueError("bad length in transaction")


class Wallet:
    def __init__(self, secret: bytes):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        if len(secret) not in (32, 64):
            raise KeyError_("the wallet key must be 64 bytes (or a 32-byte seed)")
        self._key = Ed25519PrivateKey.from_private_bytes(secret[:32])
        pub = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if len(secret) == 64 and secret[32:] != pub:
            raise KeyError_("the wallet key is damaged: its public half does not match")
        self.public = pub
        self.address = base58.b58encode(pub).decode()

    @classmethod
    def from_text(cls, text: str) -> "Wallet":
        t = (text or "").strip()
        if not t:
            raise KeyError_("no wallet key set")
        try:
            if t.startswith("["):
                raw = bytes(json.loads(t))
            else:
                raw = base58.b58decode(t)
        except Exception:  # noqa: BLE001 - never echo the key
            raise KeyError_("the wallet key is not base58 or a JSON byte array") from None
        return cls(raw)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def sign_transaction(self, tx: bytes) -> bytes:
        """Return the transaction with this wallet's signature in its signer slot."""
        n_sigs, i = _shortvec_decode(tx, 0)
        sig_start = i
        msg_start = sig_start + 64 * n_sigs
        message = tx[msg_start:]
        j = 0
        if message and message[0] & 0x80:
            j = 1                                      # versioned message prefix
        if len(message) < j + 3:
            raise ValueError("truncated message")
        required = message[j]
        n_keys, k = _shortvec_decode(message, j + 3)
        keys = [message[k + 32 * x:k + 32 * (x + 1)] for x in range(n_keys)]
        signers = keys[:required]
        if self.public not in signers:
            raise ValueError("this transaction is not for the bot's wallet")
        slot = signers.index(self.public)
        if slot >= n_sigs:
            raise ValueError("transaction has no signature slot for the bot's wallet")
        sig = self.sign(message)
        pos = sig_start + 64 * slot
        return tx[:pos] + sig + tx[pos + 64:]

    def fee_payer(self, tx: bytes) -> Optional[str]:
        n_sigs, i = _shortvec_decode(tx, 0)
        message = tx[i + 64 * n_sigs:]
        j = 1 if message and message[0] & 0x80 else 0
        n_keys, k = _shortvec_decode(message, j + 3)
        return base58.b58encode(message[k:k + 32]).decode() if n_keys else None
