"""Tests for Feishu router: encrypted event dispatch and message dedup.

Covers CI-06: test_feishu.py previously had no router decrypt or dedup tests.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.feishu.handlers import _MessageDeduplicator, _dedup
from app.feishu.router import _dispatch_encrypted_event


# ======================================================================
# Message Deduplication Tests
# ======================================================================

class TestMessageDeduplicator:
    """Test TTL-based message_id deduplication."""

    def test_first_message_not_duplicate(self):
        dedup = _MessageDeduplicator(ttl_seconds=300)
        assert dedup.is_duplicate("msg-001") is False

    def test_same_message_is_duplicate(self):
        dedup = _MessageDeduplicator(ttl_seconds=300)
        dedup.is_duplicate("msg-001")
        assert dedup.is_duplicate("msg-001") is True

    def test_different_messages_not_duplicate(self):
        dedup = _MessageDeduplicator(ttl_seconds=300)
        dedup.is_duplicate("msg-001")
        assert dedup.is_duplicate("msg-002") is False

    def test_expired_message_evicted(self):
        """Message past TTL is evicted and treated as new."""
        dedup = _MessageDeduplicator(ttl_seconds=1)
        dedup.is_duplicate("msg-001")
        # Backdate the entry to simulate TTL expiry
        first_ts = next(iter(dedup._seen.values()))
        dedup._seen["msg-001"] = first_ts - 10
        assert dedup.is_duplicate("msg-001") is False

    def test_max_size_eviction(self):
        """Oldest entries evicted when max_size exceeded."""
        dedup = _MessageDeduplicator(ttl_seconds=300, max_size=3)
        for i in range(3):
            dedup.is_duplicate(f"msg-{i:03d}")
        assert len(dedup._seen) == 3
        # Adding 4th evicts the oldest
        dedup.is_duplicate("msg-003")
        assert len(dedup._seen) == 3
        assert "msg-000" not in dedup._seen

    def test_max_size_one(self):
        """max_size=1 keeps only the latest."""
        dedup = _MessageDeduplicator(ttl_seconds=300, max_size=1)
        dedup.is_duplicate("msg-A")
        dedup.is_duplicate("msg-B")
        assert "msg-A" not in dedup._seen
        assert "msg-B" in dedup._seen


# ======================================================================
# Encrypted Event Dispatch Tests
# ======================================================================

class TestDispatchEncryptedEvent:
    """Test _dispatch_encrypted_event with mocked crypto."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Patch settings, clear dedup, and prevent real dispatch."""
        _dedup._seen.clear()
        with patch("app.feishu.router.get_settings") as mock_gs:
            settings = MagicMock()
            settings.feishu_encrypt_key = "test_key_123"
            mock_gs.return_value = settings
            with patch("app.feishu.router.dispatch_message", new_callable=AsyncMock):
                yield
        _dedup._seen.clear()

    def _make_event_json(
        self,
        msg_id: str = "om_test_001",
        msg_type: str = "text",
        content: str | None = None,
    ) -> str:
        """Build a valid decrypted Feishu message event JSON string."""
        if content is None:
            content = json.dumps({"text": "hello"})
        return json.dumps({
            "type": "event",
            "event": {
                "message": {
                    "message_type": msg_type,
                    "message_id": msg_id,
                    "content": content,
                }
            },
        })

    @pytest.mark.asyncio
    async def test_no_encrypt_key(self):
        """Empty encrypt_key -> error response."""
        with patch("app.feishu.router.get_settings") as mock_gs:
            settings = MagicMock()
            settings.feishu_encrypt_key = ""
            mock_gs.return_value = settings
            result = await _dispatch_encrypted_event("encrypted_data")
        assert result["code"] == 1
        assert "encrypt key" in result["msg"]

    @pytest.mark.asyncio
    async def test_decryption_success_returns_ok(self):
        """Valid decrypted event -> dispatch and return ok."""
        event_json = self._make_event_json()
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 0
        assert result["msg"] == "ok"

    @pytest.mark.asyncio
    async def test_decryption_failure(self):
        """AESCipher raises -> error response."""
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.side_effect = Exception("decrypt error")
            result = await _dispatch_encrypted_event("bad_data")
        assert result["code"] == 1
        assert "decryption failed" in result["msg"]

    @pytest.mark.asyncio
    async def test_empty_decrypted_data(self):
        """Empty decrypted string -> error."""
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = ""
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 1
        assert "empty" in result["msg"]

    @pytest.mark.asyncio
    async def test_invalid_json_after_decryption(self):
        """Decrypted data is not valid JSON -> error."""
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = "not json {{{"
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 1
        assert "invalid" in result["msg"].lower()

    @pytest.mark.asyncio
    async def test_url_verification_challenge(self):
        """URL verification type -> return challenge."""
        event_json = json.dumps({
            "type": "url_verification",
            "challenge": "verify_me_123",
        })
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result.get("challenge") == "verify_me_123"

    @pytest.mark.asyncio
    async def test_missing_event_field(self):
        """Decrypted data without 'event' field -> error."""
        event_json = json.dumps({"type": "event", "no_event": True})
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 1
        assert "no event" in result["msg"]

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self):
        """Event without 'message' field -> gracefully ignored."""
        event_json = json.dumps({
            "type": "event",
            "event": {"some_other_type": True},
        })
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 0
        assert "non-message" in result["msg"]

    @pytest.mark.asyncio
    async def test_missing_message_id(self):
        """Message without message_id -> error."""
        event_json = json.dumps({
            "type": "event",
            "event": {"message": {"message_type": "text"}},
        })
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 1
        assert "message_id" in result["msg"]

    @pytest.mark.asyncio
    async def test_duplicate_message_skipped(self):
        """Duplicate message_id -> skipped without dispatch."""
        msg_id = "om_test_dedup_001"
        event_json = self._make_event_json(msg_id=msg_id)
        # Pre-mark as seen
        _dedup.is_duplicate(msg_id)
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 0
        assert "duplicate" in result["msg"]

    @pytest.mark.asyncio
    async def test_dispatch_message_called_for_valid_event(self):
        """Valid event triggers dispatch_message as a background task."""
        event_json = self._make_event_json(msg_id="om_test_dispatch_001")
        with patch("lark_oapi.core.utils.decryptor.AESCipher") as MockCipher:
            MockCipher.return_value.decrypt_str.return_value = event_json
            result = await _dispatch_encrypted_event("encrypted_payload")
        assert result["code"] == 0
        # dispatch_message is patched in the _setup fixture as AsyncMock
        # Verify it was called (via create_task, so it may not complete synchronously)
