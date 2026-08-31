"""Small, dependency-free authenticated encryption for exported credentials.

The public hand-off file must remain a normal Sub2 document, so the encrypted
values are stored as strings in the existing ``password`` and ``totp_secret``
fields.  The workspace id is used directly as the key material.  A random
nonce and an HMAC tag provide semantic security and tamper detection without
adding a third-party Python dependency (the browser counterpart uses
``crypto.subtle``).

Format::

    enc:v1:<base64url nonce>:<base64url ciphertext>:<base64url tag>

This is a versioned, authenticated stream construction: HMAC-SHA256 derives
the keystream blocks and authenticates the context, nonce, and ciphertext.
It is intentionally kept in a separate module so import/export callers share
exactly the same format and can be tested independently.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct


ENCRYPTED_PREFIX = "enc:v1:"
ENCRYPTION_CONTEXT = b"gpt-auto-register:workspace-credential:v1"
NONCE_SIZE = 16
TAG_SIZE = 16


def _key_bytes(workspace_id: object) -> bytes:
    key = "" if workspace_id is None else str(workspace_id).strip()
    if not key:
        raise ValueError("workspace_id 不能为空，无法加密凭证")
    # The workspace id itself is the key.  No hidden server-side key or
    # account lookup is involved.
    return key.encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("加密凭证字段为空")
    try:
        # Decode strictly.  ``urlsafe_b64decode`` otherwise ignores some
        # non-alphabet characters, which would make malformed/tampered
        # ciphertexts look valid until much later in the login flow.
        normalized = raw.replace("-", "+").replace("_", "/")
        return base64.b64decode(
            normalized + "=" * (-len(normalized) % 4),
            validate=True,
        )
    except Exception as exc:  # pragma: no cover - implementation detail
        raise ValueError("加密凭证的 base64 格式无效") from exc


def is_encrypted_credential(value: object) -> bool:
    return str(value or "").startswith(ENCRYPTED_PREFIX)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    if length <= 0:
        return b""
    blocks = []
    counter = 0
    remaining = int(length)
    while remaining > 0:
        # Big-endian counter is mirrored by DataView.setUint32(..., false) in
        # the browser implementation.
        block = hmac.new(
            key,
            ENCRYPTION_CONTEXT + b"\x00stream\x00" + nonce + struct.pack(">I", counter),
            hashlib.sha256,
        ).digest()
        blocks.append(block)
        counter += 1
        remaining -= len(block)
    return b"".join(blocks)[:length]


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


def encrypt_credential(value: object, workspace_id: object) -> str:
    """Encrypt one credential value using the workspace id as the key.

    Empty values stay empty so the existing Sub2 shape and missing-value
    semantics are unchanged.  Already encrypted values are left untouched,
    which makes repeated exports idempotent for imported hand-off files.
    """
    plaintext = str(value or "")
    if not plaintext:
        return ""
    if is_encrypted_credential(plaintext):
        return plaintext
    key = _key_bytes(workspace_id)
    nonce = secrets.token_bytes(NONCE_SIZE)
    clear = plaintext.encode("utf-8")
    ciphertext = _xor(clear, _keystream(key, nonce, len(clear)))
    tag = hmac.new(
        key,
        ENCRYPTION_CONTEXT + b"\x00auth\x00" + nonce + ciphertext,
        hashlib.sha256,
    ).digest()[:TAG_SIZE]
    return f"{ENCRYPTED_PREFIX}{_b64encode(nonce)}:{_b64encode(ciphertext)}:{_b64encode(tag)}"


def decrypt_credential(value: object, workspace_id: object) -> str:
    """Decrypt one value, returning plaintext values unchanged.

    A malformed or tampered encrypted value raises ``ValueError`` rather than
    silently passing ciphertext into the login flow.
    """
    encoded = str(value or "")
    if not encoded or not is_encrypted_credential(encoded):
        return encoded
    key = _key_bytes(workspace_id)
    body = encoded[len(ENCRYPTED_PREFIX):]
    parts = body.split(":")
    if len(parts) != 3:
        raise ValueError("加密凭证格式无效")
    nonce = _b64decode(parts[0])
    ciphertext = _b64decode(parts[1])
    tag = _b64decode(parts[2])
    if len(nonce) != NONCE_SIZE or len(tag) != TAG_SIZE:
        raise ValueError("加密凭证长度无效")
    expected = hmac.new(
        key,
        ENCRYPTION_CONTEXT + b"\x00auth\x00" + nonce + ciphertext,
        hashlib.sha256,
    ).digest()[:TAG_SIZE]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("workspace_id 不正确或加密凭证已被篡改")
    try:
        clear = _xor(ciphertext, _keystream(key, nonce, len(ciphertext)))
        return clear.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("加密凭证解密后不是有效文本") from exc


# Explicit aliases make the intended keying scheme discoverable to callers
# and integrations without duplicating the implementation.
encrypt_with_workspace_id = encrypt_credential
decrypt_with_workspace_id = decrypt_credential
encrypt_workspace_credential = encrypt_credential
decrypt_workspace_credential = decrypt_credential
