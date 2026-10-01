"""AMP-style task mandate tokens for the workshop.

Ant's Agentic Mobile Protocol is summarised as "authorize a task, not hand over the
account": the consumer's wallet issues a scoped, time-boxed authorization to a
registered agent (with a Know-Your-Agent rating), and the acquirer enforces it.

This module is the simulator's stand-in: an HMAC-signed token
    amp1.<base64url(payload json)>.<base64url(hmac-sha256)>
The same scheme is verified in Go by checkout-api (services/checkout-api/amp.go).
Shared secret: AMP_SHARED_SECRET (demo default). A real deployment would use the
wallet's asymmetric keys published through a KYA directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid

SECRET = os.getenv("AMP_SHARED_SECRET", "tuktukpay-amp-demo-secret").encode()
PREFIX = "amp1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(*, agent_id: str, customer_id: str, wallet: str, intent: str, category: str, max_amount: float,
          currency: str, merchants: list[str], ttl_s: int = 900, max_uses: int = 1, kya_rating: str = "A",
          secret: bytes | None = None) -> tuple[str, dict]:
    now = int(time.time())
    payload = {
        "task_id": f"task_{uuid.uuid4().hex[:12]}",
        "agent_id": agent_id,
        "customer_id": customer_id,
        "wallet": wallet,
        "intent": intent,
        "category": category,
        "max_amount": float(max_amount),
        "currency": currency,
        "merchants": merchants,
        "max_uses": int(max_uses),
        "issued_at": now,
        "expires_at": now + int(ttl_s),
        "nonce": uuid.uuid4().hex[:16],
        "kya_rating": kya_rating,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret or SECRET, body.encode(), hashlib.sha256).digest())
    return f"{PREFIX}.{body}.{sig}", payload


def verify(token: str, secret: bytes | None = None) -> tuple[dict | None, str]:
    """Returns (payload, error). error is '' when the signature and format are valid."""
    try:
        prefix, body, sig = token.split(".")
    except ValueError:
        return None, "malformed"
    if prefix != PREFIX:
        return None, "unsupported_version"
    expected = _b64(hmac.new(secret or SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None, "invalid_signature"
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None, "malformed"
    return payload, ""
