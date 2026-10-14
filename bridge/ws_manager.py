"""Connection Manager: Manages active browser tabs (via WebSocket or CSP-bypassing HTTP long-polling)."""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class BrowserTab:
    """Represents a connected browser tab running the Tampermonkey script."""

    def __init__(self, tab_id: str, model: str, url: str, ws: Optional[WebSocket] = None, mode: str = "ws"):
        self.ws = ws
        self.mode = mode  # 'ws' or 'http'
        self.tab_id = tab_id
        self.model = model
        self.url = url
        self.status = "idle"  # 'idle' or 'busy'
        self.connected_at = time.time()
        self.last_heartbeat = time.time()
        self.pending_tasks: asyncio.Queue = asyncio.Queue()
        self.abort_flags: Set[str] = set()

    def touch(self):
        self.last_heartbeat = time.time()


class WSManager:
    """Coordinates connections from browser tabs and pending requests."""

    def __init__(self):
        self.tabs: Dict[str, BrowserTab] = {}
        self.active_requests: Dict[str, asyncio.Queue] = {}
        self.request_tabs: Dict[str, str] = {}  # req_id -> tab_id

    async def register(self, tab_id: str, model: str, url: str, ws: Optional[WebSocket] = None, mode: str = "ws") -> BrowserTab:
        tab = BrowserTab(tab_id=tab_id, model=model, url=url, ws=ws, mode=mode)
        self.tabs[tab_id] = tab
        logger.info(f"Browser tab registered ({mode}): {tab_id} (model: {model}, url: {url})")
        return tab

    def unregister(self, tab_id: str):
        if tab_id in self.tabs:
            del self.tabs[tab_id]
            logger.info(f"Browser tab disconnected: {tab_id}")

    def cleanup_stale_tabs(self, max_idle_sec: float = 60.0):
        now = time.time()
        stale = [tid for tid, tab in self.tabs.items() if tab.mode == "http" and (now - tab.last_heartbeat > max_idle_sec)]
        for tid in stale:
            self.unregister(tid)

    def get_available_tab(self, preferred_model: Optional[str] = None) -> Optional[BrowserTab]:
        """Find an idle tab, preferring main app tabs and matching the requested model."""
        self.cleanup_stale_tabs()
        # Filter out iframes
        valid_tabs = [
            t for t in self.tabs.values()
            if not ("bscframe" in t.url or "google.com/_/" in t.url)
        ]
        if not valid_tabs:
            valid_tabs = list(self.tabs.values())

        idle_tabs = [t for t in valid_tabs if t.status == "idle"]
        if not idle_tabs:
            if valid_tabs:
                return valid_tabs[0]
            return None

        if preferred_model:
            for t in idle_tabs:
                if preferred_model.lower() in t.model.lower():
                    return t
        return idle_tabs[0]

    def create_request_channel(self, req_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.active_requests[req_id] = queue
        return queue

    def cleanup_request(self, req_id: str):
        tab_id = self.request_tabs.pop(req_id, None)
        if tab_id and tab_id in self.tabs:
            tab = self.tabs[tab_id]
            tab.status = "idle"
            tab.abort_flags.discard(req_id)
        self.active_requests.pop(req_id, None)

    async def send_to_tab(self, tab: BrowserTab, payload: Dict[str, Any]) -> bool:
        """Send a JSON command to the browser tab (via WebSocket or HTTP polling queue)."""
        tab.touch()
        if tab.mode == "ws":
            if not tab.ws:
                return False
            try:
                await tab.ws.send_text(json.dumps(payload))
                return True
            except Exception as e:
                logger.error(f"Failed to send to tab {tab.tab_id}: {e}")
                self.unregister(tab.tab_id)
                return False
        else:
            # HTTP long-polling mode
            await tab.pending_tasks.put(payload)
            return True

    async def abort_generation(self, req_id: str):
        """Signal the browser to halt generation immediately."""
        tab_id = self.request_tabs.get(req_id)
        if tab_id and tab_id in self.tabs:
            tab = self.tabs[tab_id]
            tab.abort_flags.add(req_id)
            if tab.mode == "ws":
                await self.send_to_tab(tab, {
                    "type": "abort_generation",
                    "request_id": req_id
                })
            else:
                logger.info(f"Abort flag set for HTTP tab {tab_id} (req: {req_id})")

    async def handle_browser_message(self, tab_id: str, data: Dict[str, Any]):
        """Handle messages received from the browser script."""
        if tab_id in self.tabs:
            self.tabs[tab_id].touch()

        msg_type = data.get("type")
        req_id = data.get("request_id")

        if msg_type == "ping":
            if tab_id in self.tabs and self.tabs[tab_id].mode == "ws":
                await self.send_to_tab(self.tabs[tab_id], {"type": "pong"})
            return

        if msg_type == "status_update":
            status = data.get("status")
            if tab_id in self.tabs and status:
                self.tabs[tab_id].status = status
            return

        if not req_id or req_id not in self.active_requests:
            logger.debug(f"Received message for unknown or completed request: {req_id}")
            return

        queue = self.active_requests[req_id]
        await queue.put(data)
