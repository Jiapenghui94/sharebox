"""密码哈希、令牌与文件指纹。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS):
    """返回 (salt_b64, hash_b64, iterations)。"""
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    if salt is None:
        salt = os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
        iterations,
    )


def verify_password(password: str, salt_b64: str, hash_b64: str, iterations: int) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """数据库只存令牌指纹，泄漏数据库也无法直接登录。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sha256_file(path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def password_problem(password: str) -> str | None:
    """返回密码不合格的原因；合格返回 None。"""
    if not password or len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 200:
        return "密码过长"
    if password.isdigit():
        return "密码不能是纯数字"
    return None
