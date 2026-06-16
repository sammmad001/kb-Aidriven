"""Unit tests for MiroMind research client and result handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.miromind_client import LENGTH_CONSTRAINT, MiroMindClient, ResearchResult


# ======================================================================
# ResearchResult Tests
# ======================================================================

class TestResearchResult:
    """Test ResearchResult dataclass and payload conversion."""

    def test_to_miromind_payload_fields(self):
        """to_miromind_payload should produce adapter-compatible fields."""
        result = ResearchResult(
            content="量子计算是一种利用量子力学原理进行计算的技术。",
            thinking_text="分析量子比特与经典比特的区别",
            total_tokens=1500,
            status="completed",
            model="mirothinker-1-7-deepresearch",
            duration_ms=12000,
        )
        payload = result.to_miromind_payload()

        assert payload["content"] == result.content
        assert payload["thinking_text"] == result.thinking_text
        assert payload["total_tokens"] == 1500
        assert payload["status"] == "completed"
        assert payload["model"] == result.model
        assert payload["session_model"] == result.model
        assert "session_id" in payload
        assert "message_id" in payload

    def test_to_miromind_payload_with_tool_events(self):
        """Payload should include tool_events when present."""
        events = [{"type": "web_search", "name": "web_search", "arguments": {}, "content": "..."}]
        result = ResearchResult(
            content="Test content",
            thinking_text="",
            total_tokens=100,
            status="completed",
            model="test-model",
            duration_ms=500,
            tool_events=events,
        )
        payload = result.to_miromind_payload()
        assert payload["tool_events"] == events

    def test_error_result_fields(self):
        """Error ResearchResult should carry error message."""
        result = ResearchResult(
            content="",
            thinking_text="",
            total_tokens=0,
            status="error",
            model="test-model",
            duration_ms=0,
            error="Connection refused",
        )
        assert result.error is not None
        assert "Connection refused" in result.error


# ======================================================================
# MiroMindClient Tests
# ======================================================================

class TestMiroMindClient:
    """Test MiroMindClient behavior."""

    @pytest.fixture
    def configured_client(self) -> MiroMindClient:
        """Client with API key set."""
        with patch("app.services.miromind_client.get_settings") as mock_settings:
            mock_settings.return_value.miromind_api_key = "test-key"
            mock_settings.return_value.miromind_api_base = "https://api.miromind.ai/v1"
            mock_settings.return_value.miromind_default_model = "test-model"
            mock_settings.return_value.miromind_request_timeout = 30.0
            return MiroMindClient()

    @pytest.fixture
    def unconfigured_client(self) -> MiroMindClient:
        """Client without API key."""
        with patch("app.services.miromind_client.get_settings") as mock_settings:
            mock_settings.return_value.miromind_api_key = ""
            mock_settings.return_value.miromind_api_base = "https://api.miromind.ai/v1"
            mock_settings.return_value.miromind_default_model = "test-model"
            mock_settings.return_value.miromind_request_timeout = 30.0
            return MiroMindClient()

    def test_is_configured_true(self, configured_client: MiroMindClient):
        assert configured_client.is_configured is True

    def test_is_configured_false(self, unconfigured_client: MiroMindClient):
        assert unconfigured_client.is_configured is False

    @pytest.mark.asyncio
    async def test_research_without_api_key_returns_error(self, unconfigured_client: MiroMindClient):
        """Research without API key should return error result immediately."""
        result = await unconfigured_client.research("test question")

        assert result.status == "error"
        assert "not configured" in (result.error or "").lower()
        assert result.content == ""

    @pytest.mark.asyncio
    async def test_research_injects_length_constraint(self, configured_client: MiroMindClient):
        """Research should inject LENGTH_CONSTRAINT into the input."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "completed",
            "model": "test-model",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "Research result"}]},
            ],
            "usage": {"total_tokens": 500},
        }

        captured_payload = {}

        async def mock_post(url, json, headers):
            captured_payload.update(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            await configured_client.research("量子计算")

        # Verify LENGTH_CONSTRAINT was injected
        assert LENGTH_CONSTRAINT in captured_payload["input"]
        assert "量子计算" in captured_payload["input"]
        assert captured_payload["stream"] is False

    @pytest.mark.asyncio
    async def test_research_parses_response(self, configured_client: MiroMindClient):
        """Research should correctly parse a valid API response."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={
            "status": "completed",
            "model": "mirothinker-1-7-deepresearch",
            "output": [
                {
                    "type": "reasoning",
                    "summary": [{"text": "Analyzing the question..."}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "量子计算利用量子叠加..."}],
                },
            ],
            "usage": {"total_tokens": 1200},
        })

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await configured_client.research("量子计算")

        assert result.status == "completed"
        assert "量子叠加" in result.content
        assert "Analyzing" in result.thinking_text
        assert result.total_tokens == 1200
        assert result.model == "mirothinker-1-7-deepresearch"

    @pytest.mark.asyncio
    async def test_research_http_error(self, configured_client: MiroMindClient):
        """HTTP non-200 should return error result."""
        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await configured_client.research("test")

        assert result.status == "error"
        assert "500" in (result.error or "")

    @pytest.mark.asyncio
    async def test_health_check(self, configured_client: MiroMindClient):
        assert await configured_client.health_check() is True


# ======================================================================
# LENGTH_CONSTRAINT Tests
# ======================================================================

class TestLengthConstraint:
    """Test that LENGTH_CONSTRAINT is properly defined."""

    def test_length_constraint_exists(self):
        assert isinstance(LENGTH_CONSTRAINT, str)
        assert len(LENGTH_CONSTRAINT) > 0

    def test_length_constraint_mentions_2000(self):
        """Prompt should mention 2000 character limit."""
        assert "2000" in LENGTH_CONSTRAINT
