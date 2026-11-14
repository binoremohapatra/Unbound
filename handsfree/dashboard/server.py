"""Interactive FastAPI and WebSocket telemetry & accessible AI Assistant server."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

# Ensure project root (c:\Unbound) is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from handsfree.core.osk_engine import OnScreenKeyboardEngine, KEYBOARD_LAYOUT
from handsfree.core.cursor_tracker import CursorTracker, DwellAction
from handsfree.core.command_router import CommandRouter
from handsfree.core.voice_engine import VoiceCommandEngine
from handsfree.core.screen_understanding import ScreenUnderstandingService
from handsfree.core.guardian import SoftwareGuardian
from handsfree.core.personalization import AdaptivePersonalizationService

logger = logging.getLogger("handsfree.dashboard")

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, JSONResponse
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


class DashboardServer:
    """Provides real-time telemetry over WebSockets to local browser HUD,

    including OptiKey Smart OSK, AI Voice Commands, Screen Understanding,
    Software Guardian Auto-Recovery, and Adaptive Personalization.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        cursor_tracker: Optional[CursorTracker] = None,
        gateway: Optional[Any] = None,
        watchdog: Optional[Any] = None,
        on_assistive_cmd: Optional[Callable[[str, Any], None]] = None,
    ):
        self.host = host
        self.port = port
        self.active_connections: Set[WebSocket] = set()
        self.gateway = gateway
        self.watchdog = watchdog
        self.on_assistive_cmd = on_assistive_cmd

        # 1. Smart Accessible Keyboard
        self.osk = OnScreenKeyboardEngine(
            on_text_updated=self._on_osk_text_changed,
            on_key_actuated=self._on_osk_key_actuated,
        )

        # 2. Cursor Tracker
        self.cursor_tracker = cursor_tracker or CursorTracker(auto_calibrate=True)

        # 3. AI Adaptive Personalization
        self.personalization = AdaptivePersonalizationService()

        # 4. AI Screen Understanding
        self.screen_assistant = ScreenUnderstandingService(tts_speaker=self.osk.speak_text)

        # 5. Centralized Command Router
        self.command_router = CommandRouter(
            on_assistive_action=self._handle_assistive_action,
            tts_speaker=self.osk.speak_text,
        )

        # 6. AI Voice Command Engine
        self.voice_engine = VoiceCommandEngine(
            command_router=self.command_router,
            on_command_recognized=self._on_voice_command_recognized,
            on_status_changed=self._on_voice_status_changed,
        )

        # 7. AI Software Guardian & Auto-Recovery
        self.guardian = SoftwareGuardian(
            gateway_ref=self.gateway,
            watchdog_ref=self.watchdog,
            tts_speaker=self.osk.speak_text,
            on_status_update=self._on_guardian_status_update,
        )

        # Shared Telemetry State
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
                "quick_phrases": self.osk.get_quick_phrases(),
                "personal_words": self.osk.smart_predictor.personal_words,
            },
            "voice": self.voice_engine.get_telemetry_dict(),
            "guardian": self.guardian.get_telemetry_dict(),
            "personalization": self.personalization.get_telemetry_dict(),
            "screen": {"last_result": "", "element_highlight": None},
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
            "quick_phrases": self.osk.get_quick_phrases(),
            "personal_words": self.osk.smart_predictor.personal_words,
        })

    def _on_osk_key_actuated(self, key: str) -> None:
        logger.info(f"Dashboard OSK Actuated: {key}")
        self.personalization.record_dwell_success()

    def _on_voice_command_recognized(self, text: str, success: bool, feedback: str) -> None:
        self.personalization.record_command(text)
        self.update_telemetry("voice", {
            **self.voice_engine.get_telemetry_dict(),
            "last_spoken": text,
            "last_success": success,
            "last_feedback": feedback,
        })

    def _on_voice_status_changed(self, status: str, detail: str) -> None:
        self.update_telemetry("voice", self.voice_engine.get_telemetry_dict())

    def _on_guardian_status_update(self, status_dict: Dict[str, Any]) -> None:
        self.update_telemetry("guardian", status_dict)

    def _handle_assistive_action(self, action_name: str, payload: Any = None) -> None:
        """Dispatches assistive commands from voice/intent to subsystem handlers."""
        logger.info(f"Assistive action requested: {action_name}")
        if action_name == "show_keyboard":
            self.update_telemetry("osk_visibility", True)
        elif action_name == "hide_keyboard":
            self.update_telemetry("osk_visibility", False)
        elif action_name == "recenter_cursor":
            self.cursor_tracker.recenter()
        elif action_name == "emergency_stop":
            self.guardian.trigger_safe_stop("Voice Emergency Stop")
        elif action_name == "read_screen":
            res = self.screen_assistant.read_screen()
            self.update_telemetry("screen", {"last_result": res, "element_highlight": None})
        elif action_name == "activate_fallback":
            self.guardian.active_mode = "webcam_fallback"
            self.update_telemetry("guardian", self.guardian.get_telemetry_dict())
        elif action_name == "status":
            g = self.guardian.get_telemetry_dict()
            msg = f"Unbound Handsfree active in {g['active_mode']} mode. Tracking is stable."
            self.osk.speak_text(msg)

        if self.on_assistive_cmd:
            try:
                self.on_assistive_cmd(action_name, payload)
            except Exception as e:
                logger.error(f"Error in on_assistive_cmd handler: {e}")

    def _setup_app(self) -> None:
        self.app = FastAPI(title="Handsfree HUD Dashboard", docs_url=None, redoc_url=None)
        static_dir = Path(__file__).parent / "static"

        @self.app.get("/")
        async def get_index():
            return FileResponse(static_dir / "index.html")

        @self.app.get("/arena")
        async def get_arena():
            return FileResponse(static_dir / "arena" / "index.html")

        @self.app.get("/api/commands")
        async def get_commands():
            return JSONResponse(self.command_router.get_command_catalog())

        @self.app.get("/api/personalization")
        async def get_personalization():
            return JSONResponse(self.personalization.get_telemetry_dict())

        @self.app.get("/api/quick-phrases")
        async def get_quick_phrases():
            return JSONResponse(self.osk.get_quick_phrases())

        @self.app.post("/api/telemetry")
        async def post_telemetry(payload: Dict[str, Any]):
            self.update_telemetry("arena_telemetry", payload)
            return {"status": "ok"}

        @self.app.get("/api/keyboard-layout")
        async def get_layout():
            rows = []
            for row in KEYBOARD_LAYOUT:
                rows.append([{"label": k.label, "type": k.action_type, "width": k.width} for k in row])
            return {"rows": rows}

        @self.app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()
            self.active_connections.add(websocket)
            try:
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

        # OSK handlers
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
                "quick_phrases": self.osk.get_quick_phrases(),
                "personal_words": self.osk.smart_predictor.personal_words,
            })

        elif mtype == "osk_quick_phrase":
            phrase = msg.get("phrase", "")
            self.osk.apply_quick_phrase(phrase)

        elif mtype == "osk_add_word":
            word = msg.get("word", "")
            self.osk.add_personal_word(word)
            self.update_telemetry("osk", {
                "text": self.osk.text_buffer,
                "predictions": self.osk.predictions,
                "active_key": self.osk.active_key,
                "dwell_progress": 0.0,
                "quick_phrases": self.osk.get_quick_phrases(),
                "personal_words": self.osk.smart_predictor.personal_words,
            })

        elif mtype == "osk_remove_word":
            word = msg.get("word", "")
            self.osk.remove_personal_word(word)
            self.update_telemetry("osk", {
                "text": self.osk.text_buffer,
                "predictions": self.osk.predictions,
                "active_key": self.osk.active_key,
                "dwell_progress": 0.0,
                "quick_phrases": self.osk.get_quick_phrases(),
                "personal_words": self.osk.smart_predictor.personal_words,
            })

        # Voice handlers
        elif mtype == "voice_command":
            spoken_text = msg.get("text", "")
            self.voice_engine.submit_spoken_text(spoken_text)

        elif mtype == "voice_toggle":
            if self.voice_engine.is_running:
                self.voice_engine.stop()
            else:
                self.voice_engine.start()
            self.update_telemetry("voice", self.voice_engine.get_telemetry_dict())

        # Screen Understanding handlers
        elif mtype == "screen_read":
            result = self.screen_assistant.read_screen()
            self.update_telemetry("screen", {"last_result": result, "element_highlight": None})

        elif mtype == "screen_find":
            query = msg.get("query", "")
            found_elem = self.screen_assistant.find_element(query)
            highlight = None
            if found_elem:
                highlight = {
                    "name": found_elem.name,
                    "x": found_elem.x,
                    "y": found_elem.y,
                    "w": found_elem.width,
                    "h": found_elem.height,
                }
            self.update_telemetry("screen", {
                "last_result": f"Located '{query}'" if found_elem else f"'{query}' not found",
                "element_highlight": highlight,
            })

        elif mtype == "screen_explain":
            result = self.screen_assistant.explain_dialog()
            self.update_telemetry("screen", {"last_result": result, "element_highlight": None})

        # Guardian handlers
        elif mtype == "emergency_stop":
            self.guardian.trigger_safe_stop("User Emergency Button Click")
            self.update_telemetry("guardian", self.guardian.get_telemetry_dict())

        # Personalization handlers
        elif mtype == "apply_recommendation":
            rec_id = msg.get("id", "")
            self.personalization.apply_recommendation(rec_id)
            self.update_telemetry("personalization", self.personalization.get_telemetry_dict())

        elif mtype == "dismiss_recommendation":
            rec_id = msg.get("id", "")
            self.personalization.dismiss_recommendation(rec_id)
            self.update_telemetry("personalization", self.personalization.get_telemetry_dict())

        elif mtype == "update_setting":
            setting = msg.get("setting", "")
            val = msg.get("value")
            self.personalization.update_setting(setting, val)
            self.update_telemetry("personalization", self.personalization.get_telemetry_dict())

        elif mtype == "reset_profile":
            self.personalization.reset_defaults()
            self.update_telemetry("personalization", self.personalization.get_telemetry_dict())

        # Dwell & Navigation
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

        # Start background voice engine and guardian
        self.voice_engine.start()
        self.guardian.start()

        def run_server():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning", loop="asyncio")
            server = uvicorn.Server(config)
            self._loop.run_until_complete(server.serve())

        self._thread = threading.Thread(target=run_server, daemon=True, name="DashboardThread")
        self._thread.start()
        logger.info(f"Dashboard server running at http://{self.host}:{self.port}")

    def stop(self) -> None:
        """Stops the background event loop, voice engine, guardian, and WebSocket server."""
        self.voice_engine.stop()
        self.guardian.stop()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("Dashboard server stopped.")
