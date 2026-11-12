"""Password hashing. scrypt is used because this tree has no third-party packages.

argon2id is not in the Python standard library. scrypt with the parameters
below is the supported password hash. Hashes are stored; passwords are not.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from ssios.errors import SsiosError

# n=2**14 is the baseline that stays practical on a 24GB machine without a
# native argon2 module. r and p follow the standard scrypt recommendation.
_N = 1 << 14
_R = 8
_P = 1
_DKLEN = 32


def validate_password(password: str, username: str) -> None:
    if not isinstance(password, str) or len(password) < 12 or len(password) > 200:
        raise SsiosError("password_rejected", "password must be 12 to 200 characters", 400)
    if password.casefold() == username.casefold():
        raise SsiosError("password_rejected", "password must not match the username", 400)
    if "\x00" in password or "\n" in password or "\r" in password:
        raise SsiosError("password_rejected", "password rejected", 400)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n_text, r_text, p_text, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        n_value, r_value, p_value = int(n_text), int(r_text), int(p_text)
        if n_value < _N or r_value < 1 or p_value < 1 or n_value > (1 << 20):
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) < 16 or len(expected) < 16:
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n_value, r=r_value, p=p_value, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)
