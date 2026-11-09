"""Signed cookie for the dashboard. The company comes from this cookie, never from the form."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

COOKIE = "emo_session"
_MAX_AGE = 60 * 60 * 24 * 7


def session_secret() -> bytes:
    env = os.getenv("SESSION_SECRET", "").strip()
    if env:
        return env.encode()
    if os.getenv("FUNCTIONS_WORKER_RUNTIME"):
        raise RuntimeError("Set SESSION_SECRET on the Function App before anyone signs in.")
    from pipeline.config import settings

    path = settings.root / "data" / "session.secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes().strip()
    secret = os.urandom(32).hex().encode()
    path.write_bytes(secret + b"\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return secret


def encode_session(payload: dict) -> str:
    body_data = dict(payload)
    body_data["exp"] = int(time.time()) + _MAX_AGE
    raw = json.dumps(body_data, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(session_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def decode_session(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(session_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload


def read_cookie(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value
    return ""


def set_cookie(token: str, *, secure: bool) -> str:
    parts = [
        f"{COOKIE}={token}",
        "HttpOnly",
        "SameSite=Lax",
        "Path=/",
        f"Max-Age={_MAX_AGE}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie() -> str:
    return f"{COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"


def secret_file() -> Path:
    from pipeline.config import settings

    return settings.root / "data" / "session.secret"
