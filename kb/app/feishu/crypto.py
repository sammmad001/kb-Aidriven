"""Feishu webhook signature verification and message decryption."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import base64

logger = logging.getLogger(__name__)


def verify_signature(
    verification_token: str,
    timestamp: str,
    nonce: str,
    body: str,
    signature: str,
) -> bool:
    """Verify Feishu webhook request signature.

    Signature = Base64(SHA256(timestamp + nonce + body))
    Note: Feishu uses the verification token as part of the signing process.
    """
    if not all([timestamp, nonce, body, signature]):
        return False

    # Build the string to sign
    sign_base = f"{timestamp}{nonce}{verification_token}{body}"
    computed = hashlib.sha256(sign_base.encode("utf-8")).hexdigest()

    # Compare
    try:
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


def decrypt_message(encrypt_key: str, encrypted_data: str) -> str:
    """Decrypt Feishu encrypted message data using AES-256-CBC.

    Key = SHA256(encrypt_key)[:32] (first 16 bytes as AES key)
    IV = first 16 bytes of encrypted data (after base64 decode)
    """
    if not encrypt_key or not encrypted_data:
        return encrypted_data

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding

        # Derive AES key from encrypt_key
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()[:16]

        # Base64 decode
        encrypted_bytes = base64.b64decode(encrypted_data)

        # Extract IV (first 16 bytes) and ciphertext
        iv = encrypted_bytes[:16]
        ciphertext = encrypted_bytes[16:]

        # Decrypt
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # Remove PKCS7 padding
        unpadder = sym_padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()

        return plaintext.decode("utf-8")
    except ImportError:
        logger.warning("cryptography package not installed, skipping decryption")
        return encrypted_data
    except Exception as exc:
        logger.error("Decryption failed: %s", exc)
        return encrypted_data
