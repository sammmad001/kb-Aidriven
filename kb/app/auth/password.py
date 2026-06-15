"""Password hashing and verification using bcrypt directly.

We use the bcrypt library directly (instead of passlib) to avoid
compatibility issues between passlib 1.7.x and bcrypt >= 4.1.
"""

from __future__ import annotations

import bcrypt


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt."""
    # bcrypt has a 72-byte limit; truncate to avoid ValueError
    raw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        raw = plain.encode("utf-8")[:72]
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except Exception:
        return False
