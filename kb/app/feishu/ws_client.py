"""Feishu WebSocket long-connection client for event subscription."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.ws import Client as WsClient

from app.config import Settings
from app.feishu.handlers import _dedup, dispatch_message

logger = logging.getLogger(__name__)


class FeishuWsClient:
    """Feishu WebSocket long-connection client.

    Connects to Feishu's event subscription service via WebSocket.
    Receives events (e.g., im.message.receive_v1) and dispatches them
    to the shared message handlers.
    """

    def __init__(self, settings: Settings) -> None:
        self._app_id = settings.feishu_app_id
        self._app_secret = settings.feishu_app_secret
        self._ws_client: WsClient | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # Connection state tracking
        self._connected = False
        self._last_disconnect: float = 0
        self._reconnect_attempts: int = 0

    async def start(self) -> None:
        """Start the WebSocket long-connection client."""
        if not self._app_id or not self._app_secret:
            logger.warning("Feishu App ID/Secret not configured, skipping WebSocket client")
            return

        logger.info("Starting Feishu WebSocket client...")

        # Capture the main event loop reference for thread-safe dispatching
        self._main_loop = asyncio.get_running_loop()

        # Create event dispatcher handler
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )

        # Create WebSocket client with auto-reconnect enabled
        self._ws_client = WsClient(
            app_id=self._app_id,
            app_secret=self._app_secret,
            log_level=lark.LogLevel.INFO,
            event_handler=handler,
            auto_reconnect=True,
        )
        # Register reconnect lifecycle callbacks
        self._ws_client.on_reconnecting = self._on_reconnecting
        self._ws_client.on_reconnected = self._on_reconnected
        self._running = True
        self._connected = True

        # Run in a separate thread with its own event loop
        self._thread = threading.Thread(target=self._run_ws_thread, daemon=True)
        self._thread.start()

        # Start watchdog to detect thread crashes
        asyncio.create_task(self._watchdog())
        logger.info("Feishu WebSocket client started")

    def _run_ws_thread(self) -> None:
        """Run the WebSocket client in a dedicated thread."""
        try:
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # The ws client start() is blocking and runs its own loop
            self._ws_client.start()
        except Exception as exc:
            logger.exception("Feishu WebSocket client error: %s", exc)

    async def stop(self) -> None:
        """Stop the WebSocket client."""
        self._running = False
        self._connected = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                pass
            self._ws_client = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._main_loop = None
        logger.info("Feishu WebSocket client stopped")

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket is actually connected (not just _running flag)."""
        return self._connected and self._running

    # ==================================================================
    # Reconnect Callbacks
    # ==================================================================

    def _on_reconnecting(self) -> None:
        """Called by SDK when connection is lost and reconnect begins."""
        self._connected = False
        self._last_disconnect = time.time()
        self._reconnect_attempts += 1
        logger.warning(
            "Feishu WS connection lost, reconnecting (attempt #%d)",
            self._reconnect_attempts,
        )

    def _on_reconnected(self) -> None:
        """Called by SDK when reconnect succeeds."""
        self._connected = True
        logger.info("Feishu WS reconnected successfully")

    # ==================================================================
    # Watchdog
    # ==================================================================

    async def _watchdog(self) -> None:
        """Periodically check if WS thread is alive; restart if dead."""
        while self._running:
            await asyncio.sleep(60)
            if self._running and self._thread and not self._thread.is_alive():
                logger.error("Feishu WS thread died unexpectedly, restarting...")
                self._connected = False
                # Reset and restart
                self._thread = threading.Thread(target=self._run_ws_thread, daemon=True)
                self._thread.start()
                logger.info("Feishu WS thread restarted")

    # ==================================================================
    # Event Handlers
    # ==================================================================

    def _on_message(self, data: Any) -> None:
        """Handle incoming message event (im.message.receive_v1).

        This is called by the lark-oapi event dispatcher in the WS thread.
        We dispatch the coroutine to the main event loop via call_soon_threadsafe.
        """
        try:
            message = data.event.message
            msg_type = message.message_type
            message_id = message.message_id

            # Dedup check: skip if this message_id was already processed
            if _dedup.is_duplicate(message_id):
                logger.info("Duplicate message_id %s, skipping", message_id)
                return

            # Parse content
            content_str = message.content
            try:
                content = json.loads(content_str) if isinstance(content_str, str) else content_str
            except json.JSONDecodeError:
                content = {}

            # Thread-safe dispatch to the main event loop
            if self._main_loop and self._main_loop.is_running():
                self._main_loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    dispatch_message(msg_type, content, message_id),
                )
            else:
                logger.warning("Main event loop not available, dropping message %s", message_id)
        except Exception as exc:
            logger.exception("Failed to handle message event: %s", exc)
