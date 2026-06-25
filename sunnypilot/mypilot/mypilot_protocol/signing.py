"""Signed HTTP request helpers for authenticated device -> Stack calls.

A device signs the canonical string ``METHOD\\nPATH\\nTIMESTAMP\\nSHA256_HEX(body)`` with its
Ed25519 private key. The server recomputes the string and verifies against the stored public
key, additionally enforcing a freshness window on the timestamp to prevent replay.
"""

from __future__ import annotations

import hashlib
import time

from .crypto import sign, verify

DEVICE_HEADER = "X-MyPilot-Device"
TIMESTAMP_HEADER = "X-MyPilot-Timestamp"
SIGNATURE_HEADER = "X-MyPilot-Signature"

# Maximum allowed clock skew (seconds) between device and server for a signed request.
DEFAULT_MAX_SKEW = 60


def canonical_request(method: str, path: str, timestamp: int | str, body: bytes) -> bytes:
    """Build the canonical byte string that gets signed/verified."""
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}".encode("utf-8")


def build_signed_headers(
    device_id: str,
    private_key_b64: str,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
) -> dict[str, str]:
    """Produce the X-MyPilot-* headers for an authenticated device request."""
    ts = int(time.time()) if timestamp is None else int(timestamp)
    message = canonical_request(method, path, ts, body)
    signature = sign(private_key_b64, message)
    return {
        DEVICE_HEADER: device_id,
        TIMESTAMP_HEADER: str(ts),
        SIGNATURE_HEADER: signature,
    }


def is_timestamp_fresh(timestamp: int | str, max_skew: int = DEFAULT_MAX_SKEW) -> bool:
    """Return True iff ``timestamp`` is within ``max_skew`` seconds of now."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(int(time.time()) - ts) <= max_skew


def verify_request_signature(
    public_key_b64: str,
    method: str,
    path: str,
    timestamp: int | str,
    signature_b64: str,
    body: bytes = b"",
    max_skew: int = DEFAULT_MAX_SKEW,
) -> bool:
    """Verify a signed device request: both the timestamp freshness and the signature."""
    if not is_timestamp_fresh(timestamp, max_skew):
        return False
    message = canonical_request(method, path, timestamp, body)
    return verify(public_key_b64, signature_b64, message)
