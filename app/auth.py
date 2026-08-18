"""
auth.py — Authentication module for Nobø Web Control

Provides:
- File-based user store (data/users.json) with bcrypt-hashed passwords
- In-memory session management
- Brute-force rate limiting (5 attempts → 60 s lockout)
"""

import os
import json
import time
import secrets
from pathlib import Path
from typing import Optional

import bcrypt

# ---------------------------------------------------------------------------
# Storage paths (override in tests by patching these)
# ---------------------------------------------------------------------------
DATA_DIR = Path("data")
USERS_FILE = DATA_DIR / "users.json"

# ---------------------------------------------------------------------------
# Session store  {session_id: {"username": str, "created": float}}
# ---------------------------------------------------------------------------
sessions: dict = {}
SESSION_MAX_AGE = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Brute-force tracking  {key: {"count": int, "locked_until": float}}
# ---------------------------------------------------------------------------
login_attempts: dict = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# User store helpers
# ---------------------------------------------------------------------------

def init_user_store() -> None:
    """Create data/ directory and users.json with default admin if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not USERS_FILE.exists():
        default_users = {
            "admin": {
                "password_hash": hash_password("nobohub"),
                "role": "admin",
            }
        }
        save_users(default_users)


def load_users() -> dict:
    """Load users from file; return empty dict on error."""
    try:
        with USERS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_users(users: dict) -> None:
    """Persist users to file atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
    tmp.replace(USERS_FILE)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt and return the hash string."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Return True if *password* matches the bcrypt *hashed* value."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_session(username: str) -> str:
    """Create a new session and return the session ID."""
    session_id = secrets.token_hex(32)
    sessions[session_id] = {"username": username, "created": time.time()}
    return session_id


def get_session(session_id: str) -> Optional[dict]:
    """Return session dict if valid and not expired, else None."""
    if not session_id:
        return None
    session = sessions.get(session_id)
    if session is None:
        return None
    if time.time() - session["created"] > SESSION_MAX_AGE:
        del sessions[session_id]
        return None
    return session


def delete_session(session_id: str) -> None:
    sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(key: str) -> tuple:
    """
    Returns (allowed: bool, seconds_remaining: int).
    Tracks by username key.
    """
    entry = login_attempts.get(key)
    if entry is None:
        return True, 0
    if entry["count"] < MAX_ATTEMPTS:
        return True, 0
    remaining = entry.get("locked_until", 0) - time.time()
    if remaining <= 0:
        # Lockout expired — reset
        del login_attempts[key]
        return True, 0
    return False, int(remaining) + 1


def record_failed_attempt(key: str) -> None:
    entry = login_attempts.setdefault(key, {"count": 0, "locked_until": 0.0})
    entry["count"] += 1
    if entry["count"] >= MAX_ATTEMPTS:
        entry["locked_until"] = time.time() + LOCKOUT_SECONDS


def clear_attempts(key: str) -> None:
    login_attempts.pop(key, None)
