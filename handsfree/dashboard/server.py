"""Interactive FastAPI and WebSocket telemetry & accessible On-Screen Keyboard server."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Set

from handsfree.core.osk_engine import OnScreenKeyboardEngine, KEYBOARD_LAYOUT
from handsfree.core.cursor_tracker import CursorTracker, DwellAction

logger = logging.getLogger("handsfree.dashboard")

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


class DashboardServer:
    """Provides real-time telemetry over WebSockets to a local browser HUD,

    including OptiKey On-Screen Keyboard and Tracky-Mouse dwell clicking.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self.host = host
        self.port = port
        self.active_connections: Set[WebSocket] = set()

        # Shared OptiKey engine & Cursor tracker
        self.osk = OnScreenKeyboardEngine(
            on_text_updated=self._on_osk_text_changed,
            on_key_actuated=self._on_osk_key_actuated,
        )
        self.cursor_tracker = CursorTracker()

        self.latest_telemetry: Dict[str, Any] = {
            "timestamp": time.time(),
            "state": "active",
            "pose": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
            "stick": {"x": 0.0, "y": 0.0},
            "cursor": {"x": 960, "y": 540, "dx": 0, "dy": 0},
            "dwell": {"state": "idle", "progress": 0.0, "action": "left_click", "dragging": False},
            "osk": {
                "text": "",
                "predictions": self.osk.predictions,
                "active_key": None,
                "dwell_progress": 0.0,
            },
            "blendshapes": {},
            "pressed": [],
            "audio": {"pop": False, "speech": False},
        }

        self.app = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if HAS_FASTAPI:
            self._setup_app()

    def _on_osk_text_changed(self, text: str) -> None:
        self.update_telemetry("osk", {
            "text": text,
            "predictions": self.osk.predictions,
            "active_key": self.osk.active_key,
            "dwell_progress": self.osk.dwell_progress,
        })

    def _on_osk_key_actuated(self, key: str) -> None:
        logger.info(f"Dashboard OSK Actuated: {key}")

    def _setup_app(self) -> None:
        self.app = FastAPI(title="Handsfree HUD Dashboard", docs_url=None, redoc_url=None)
        static_dir = Path(__file__).parent / "static"

        @self.app.get("/")
        async def get_index():
            return FileResponse(static_dir / "index.html")

        @self.app.get("/api/keyboard-layout")
        async def get_layout():
            # Return serialized layout definition
            rows = []
            for row in KEYBOARD_LAYOUT:
                rows.append([{"label": k.label, "type": k.action_type, "width": k.width} for k in row])
            return {"rows": rows}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.add(websocket)
            try:
                # Send immediate state on connection
                await websocket.send_text(json.dumps(self.latest_telemetry))
                while True:
                    raw_msg = await websocket.receive_text()
                    try:
                        msg = json.loads(raw_msg)
                        self._handle_client_message(msg)
                    except Exception as e:
                        logger.error(f"Error handling client WS message: {e}")
            except WebSocketDisconnect:
                self.active_connections.discard(websocket)
            except Exception:
                self.active_connections.discard(websocket)

        if static_dir.exists():
            self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _handle_client_message(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == "osk_key":
            key = msg.get("key", "")
            self.osk.actuate_key(key)

        elif mtype == "osk_predict":
            word = msg.get("word", "")
            self.osk.select_prediction(word)

        elif mtype == "osk_hover":
            key = msg.get("key")
            prog = self.osk.hover_key(key)
            self.update_telemetry("osk", {
                "text": self.osk.text_buffer,
                "predictions": self.osk.predictions,
                "active_key": self.osk.active_key,
                "dwell_progress": prog,
            })

        elif mtype == "dwell_action":
            action_name = msg.get("action", "left_click")
            try:
                act = DwellAction(action_name)
                if self.cursor_tracker.dwell_clicker:
                    self.cursor_tracker.dwell_clicker.set_action(act)
                self.update_telemetry("dwell", {
                    "state": "idle",
                    "progress": 0.0,
                    "action": action_name,
                    "dragging": self.cursor_tracker.dwell_clicker.is_dragging if self.cursor_tracker.dwell_clicker else False,
                })
            except Exception:
                pass

        elif mtype == "recenter":
            yaw = msg.get("yaw", 0.0)
            pitch = msg.get("pitch", 0.0)
            self.cursor_tracker.set_neutral(yaw, pitch)

    def update_telemetry(self, key: str, value: Any) -> None:
        self.latest_telemetry[key] = value
        self.latest_telemetry["timestamp"] = time.time()

        if self.active_connections and self._loop and self._loop.is_running():
            msg = json.dumps(self.latest_telemetry)
            asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    async def _broadcast(self, message: str) -> None:
        dead = []
        for ws in self.active_connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.active_connections.discard(d)

    def start(self) -> None:
        if not HAS_FASTAPI:
            logger.warning("FastAPI or Uvicorn not installed. Dashboard server disabled.")
            return

        def run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", loop="asyncio")
            server = uvicorn.Server(config)
            self._loop.run_until_complete(server.serve())

        self._thread = threading.Thread(target=run_server, daemon=True, name="DashboardThread")
        self._thread.start()
        logger.info(f"Dashboard server running at http://{self.host}:{self.port}")
