"""Read-only FastAPI and WebSocket telemetry dashboard server."""

import asyncio
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Dict, Optional, Set

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
    """Provides real-time telemetry over WebSockets to a local browser HUD."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self.host = host
        self.port = port
        self.active_connections: Set[WebSocket] = set()
        self.latest_telemetry: Dict[str, Any] = {
            "timestamp": time.time(),
            "state": "active",
            "pose": {"yaw": 0.0, "pitch": 0.0, "roll": 0.0},
            "stick": {"x": 0.0, "y": 0.0},
            "blendshapes": {},
            "pressed": [],
            "audio": {"pop": False, "speech": False},
        }
        self.app = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        if HAS_FASTAPI:
            self._setup_app()

    def _setup_app(self) -> None:
        self.app = FastAPI(title="Handsfree HUD Dashboard", docs_url=None, redoc_url=None)
        static_dir = Path(__file__).parent / "static"

        @self.app.get("/")
        async def get_index():
            return FileResponse(static_dir / "index.html")

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.add(websocket)
            try:
                await websocket.send_text(json.dumps(self.latest_telemetry))
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.active_connections.discard(websocket)
            except Exception:
                self.active_connections.discard(websocket)

        if static_dir.exists():
            self.app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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

