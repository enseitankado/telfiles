"""Web-UI password gate.

Single-user app: one password, multiple browser sessions. Store a
pbkdf2_sha256 password hash and a small set of opaque session tokens in
/app/data/ui_auth.json (volume-mounted). Default password is `admin` until
the user picks a new one; deleting the JSON file resets it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Tuple

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
AUTH_PATH = os.path.join(DATA_DIR, "ui_auth.json")

DEFAULT_PASSWORD = "admin"
PBKDF2_ITERS = 200_000

SESSION_TTL  = 12 * 3600              # 12 hours when "remember" is off
REMEMBER_TTL = 30 * 24 * 3600         # 30 days when "remember" is on

COOKIE_NAME = "tf_ui_session"


# ── persistence ──────────────────────────────────────────────────────────────

def _read() -> dict:
    try:
        with open(AUTH_PATH) as f:
            return json.load(f) or {}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return {}


def _write(d: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, AUTH_PATH)


# ── password ─────────────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${h.hex()}"


def _verify_hash(hash_str: str, plain: str) -> bool:
    try:
        algo, iters_s, salt_hex, h_hex = hash_str.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(h_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
    return hmac.compare_digest(actual, expected)


def is_default_password() -> bool:
    """True if no custom password is set (i.e. password is still 'admin')."""
    return not _read().get("password_hash")


def verify_password(plain: str) -> bool:
    rec = _read()
    hashed = rec.get("password_hash")
    if not hashed:
        return plain == DEFAULT_PASSWORD
    return _verify_hash(hashed, plain or "")


def set_password(new_plain: str):
    """Set a new password. Invalidates every existing session — typing in a
    new password should kick every browser including the one doing the change."""
    if not new_plain:
        raise ValueError("password cannot be empty")
    d = _read()
    d["password_hash"] = _hash_password(new_plain)
    d["tokens"] = {}
    _write(d)


# ── tokens ───────────────────────────────────────────────────────────────────

def _hash_token(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _gc_expired(d: dict, now: int) -> dict:
    tokens = d.get("tokens") or {}
    return {k: v for k, v in tokens.items() if v.get("exp", 0) > now}


def create_token(remember: bool) -> Tuple[str, int]:
    """Return (token_plain, ttl_seconds). Caller sets the cookie."""
    t = secrets.token_urlsafe(32)
    ttl = REMEMBER_TTL if remember else SESSION_TTL
    now = int(time.time())
    d = _read()
    tokens = _gc_expired(d, now)
    tokens[_hash_token(t)] = {
        "exp": now + ttl,
        "remember": bool(remember),
        "created": now,
    }
    d["tokens"] = tokens
    _write(d)
    return t, ttl


def is_valid_token(t: Optional[str]) -> bool:
    if not t:
        return False
    rec = (_read().get("tokens") or {}).get(_hash_token(t))
    if not rec:
        return False
    return rec.get("exp", 0) > int(time.time())


def revoke_token(t: Optional[str]):
    if not t:
        return
    d = _read()
    tokens = d.get("tokens") or {}
    if tokens.pop(_hash_token(t), None) is not None:
        d["tokens"] = tokens
        _write(d)
