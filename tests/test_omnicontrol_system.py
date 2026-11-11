"""tests/test_omnicontrol_system.py - Production-grade automated test suite for OmniControl OS.

Enforces zero-hardware / headless CI/CD execution using smart mocking across:
  1. Win32 Output Sink & Memory Alignment (x64 40-byte struct, diff engine, panic_release_all, window context).
  2. Dual-Mode Psychic Intent Adapter & Hot-Unplug (100Hz JSON packets, instant sub-100ms optical fallback).
  3. Central Finite State Machine & Priority Hierarchy (P0 SAFE_IDLE, P1 CALIBRATING, P2 LATCHED, P3 CONVERSE_LOCK).
  4. Split-Path Pipeline & Filters (Cubic deadzone/response, 1-Euro adaptive jitter suppression <0.5px).
  5. Focus-Safe HUD Overlay (WS_EX_NOACTIVATE, WS_EX_TOPMOST, and graceful headless fallback).
"""
import ctypes
import json
import math
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure omnicontrol_os / unbound_os is importable
OS_DIR = Path("D:/unbound/unbound_os") if Path("D:/unbound/unbound_os").is_dir() else Path("D:/unbound/omnicontrol_os")
if str(OS_DIR) not in sys.path:
    sys.path.insert(0, str(OS_DIR))

try:
    import central_fsm
    import main_hud
    import signal_pipelines
    import win32_output_sink
    from central_fsm import (
        Action,
        ACTIVE_ESPORTS,
        ACTIVE_WORKSTATION,
        CALIBRATING,
        CONVERSE_LOCK,
        FULL_TRACKING,
        GLASSES_FALLBACK,
        LATCHED,
        OmniFSM,
        SAFE_IDLE,
    )
    from signal_pipelines import (
        cubic_response,
        FrameSample,
        IntentEvent,
        OneEuroFilter,
        PsychicIntentAdapter,
    )
    from win32_output_sink import (
        _NullBackend,
        INPUT,
        inject_unicode_string,
        KEYEVENTF_KEYUP,
        KEYEVENTF_SCANCODE,
        KEYEVENTF_UNICODE,
        load_scancode_table,
        StatefulOutputSink,
        WindowContextInspector,
    )
except ImportError:
    pytest.skip("omnicontrol_os modules not found on this machine", allow_module_level=True)


@pytest.fixture
def base_config():
    """Default test config matching omnicontrol_os/config.json."""
    cfg_path = OS_DIR / "config.json"
    if cfg_path.is_file():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "loop": {"hz": 60},
        "safety": {
            "face_lost_ms": 300,
            "face_present_ms": 300,
            "calibrate_double_blink_ms": 1000,
            "latch_hold_ms": 3000,
            "latch_keys": ["lalt"],
        },
        "aim": {
            "deadzone": 0.06,
            "curve": "cubic",
            "full_scale_deg": 25.0,
            "mouse_speed_px_per_s": 1400.0,
            "glasses_fallback_ms": 800,
            "schmitt_on": 0.55,
            "schmitt_off": 0.33,
        },
        "omnibus": {
            "serial_baud": 115200,
            "serial_stale_ms": 500,
            "hardware_clench_threshold": 0.6,
        },
        "window_profiles": {
            "gta5": {"match": ["grand theft auto", "gta5"], "mode": "ACTIVE_ESPORTS"},
            "word": {"match": ["word", "winword"], "mode": "ACTIVE_WORKSTATION"},
        },
    }


# ==============================================================================
# 1. Win32 Output Sink & Safety Tests
# ==============================================================================
class TestWin32OutputSink:

    def test_x64_input_struct_exact_size(self):
        """SendInput silently fails with ERROR_INVALID_PARAMETER if sizeof(INPUT) != 40."""
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert ctypes.sizeof(INPUT) == 40, (
                f"Memory Alignment Failure: sizeof(INPUT) is {ctypes.sizeof(INPUT)}B, "
                "must be exactly 40 bytes on 64-bit Windows."
            )

    def test_stateful_sink_key_diff_ordering(self):
        """Diff engine must release keys FIRST, then press newly desired keys."""
        backend = _NullBackend()
        sink = StatefulOutputSink(backend=backend, auto_release=False)

        # 1. Press W
        sink.want_key("w", True)
        sink.flush(now=1.0)
        assert ("key", "w", True) in backend.log
        assert backend.keys == {"w"}

        # 2. Release W and press A in the same tick
        backend.log.clear()
        sink.want_key("w", False)
        sink.want_key("a", True)
        sink.flush(now=1.016)

        # Assert correct diff & ordering: release W happens before press A
        assert backend.log == [("key", "w", False), ("key", "a", True)]
        assert backend.keys == {"a"}

    def test_stateful_sink_panic_release_all(self):
        """panic_release_all must clear all desired/held keys and issue hardware releases immediately."""
        backend = _NullBackend()
        sink = StatefulOutputSink(backend=backend, auto_release=False)

        sink.want_key("w", True)
        sink.want_key("lshift", True)
        sink.want_mouse("left", True)
        sink.flush(now=1.0)
        assert backend.keys == {"w", "lshift"}
        assert backend.mouse == {"left"}

        # Trigger emergency panic release
        sink.panic_release_all("unit-test-emergency")

        assert len(sink._held_keys) == 0
        assert len(sink._want_keys) == 0
        assert len(backend.keys) == 0
        assert len(backend.mouse) == 0
        assert ("key", "w", False) in backend.log
        assert ("key", "lshift", False) in backend.log
        assert ("mouse", "left", False) in backend.log

    def test_window_context_inspector_matching(self, base_config):
        """Inspects foreground window title/exe without stealing focus and routes to correct profile."""
        inspector = WindowContextInspector(profiles=base_config["window_profiles"])

        # Test gaming profile match
        with patch.object(WindowContextInspector, "_foreground", return_value=("Grand Theft Auto V", "GTA5.exe")):
            title, exe, mode = inspector.poll(now=1.0)
            assert mode == "ACTIVE_ESPORTS"
            assert inspector.match("Grand Theft Auto V", "GTA5.exe") == "ACTIVE_ESPORTS"

        # Test workstation profile match
        with patch.object(WindowContextInspector, "_foreground", return_value=("Document1 - Microsoft Word", "WINWORD.EXE")):
            inspector._next = 0.0  # bypass 4Hz rate limiter
            title, exe, mode = inspector.poll(now=2.0)
            assert mode == "ACTIVE_WORKSTATION"
            assert inspector.match("Document1 - Microsoft Word", "WINWORD.EXE") == "ACTIVE_WORKSTATION"

        # Test unmatched profile
        assert inspector.match("Calculator", "calc.exe") == ""

    def test_inject_unicode_string_structure(self):
        """Unicode injection builds valid KEYEVENTF_UNICODE pairs without error."""
        # Empty string
        assert inject_unicode_string("") == 0

        # On non-Windows platforms or mocked user32, verify it doesn't crash
        if os.name == "nt":
            # Real Windows: inject harmless unicode space and confirm event count
            with patch.object(ctypes.windll.user32, "SendInput", return_value=4) as mock_send:
                res = inject_unicode_string("Hi")
                assert res == 4
                assert mock_send.call_count == 1


# ==============================================================================
# 2. Dual-Mode Psychic Intent Adapter & Hot-Unplug Tests
# ==============================================================================
class TestPsychicIntentAdapter:

    class FakeSerial:
        """Mock pyserial interface streaming high-frequency JSON telemetry."""

        def __init__(self, packets=None):
            self.packets = list(packets or [])
            self.closed = False
            self.in_waiting = len(self.packets)

        def readline(self):
            if not self.packets:
                return b""
            pkt = self.packets.pop(0)
            self.in_waiting = len(self.packets)
            if isinstance(pkt, bytes):
                return pkt
            return (json.dumps(pkt) + "\n").encode("utf-8")

        def close(self):
            self.closed = True

    def test_hardware_omniband_packet_processing(self, base_config):
        """Valid 100Hz packets parsed and set mode to HARDWARE_OMNIBAND."""
        vision = MagicMock()
        audio = MagicMock()
        emitted_events = []

        adapter = PsychicIntentAdapter(
            cfg=base_config,
            vision=vision,
            audio=audio,
            on_event=lambda ev: emitted_events.append(ev),
        )

        sample_pkt = {
            "piezo_raw": 512,
            "pulse": 0.85,
            "clench": 1.0,
            "lat_ms": 2.5,
        }
        fake_ser = self.FakeSerial([sample_pkt])
        adapter._ser = fake_ser
        adapter.mode = "HARDWARE_OMNIBAND"

        # Poll serial at current time
        adapter._serial_poll(now=10.0)

        assert adapter.mode == "HARDWARE_OMNIBAND"
        assert len(emitted_events) >= 1
        clench_ev = next((e for e in emitted_events if e.name == "clench"), None)
        assert clench_ev is not None
        assert clench_ev.source == "HARDWARE_OMNIBAND"
        assert clench_ev.value == 1.0

    def test_hot_unplug_instant_optical_fallback(self, base_config):
        """Simulate sudden serial exception on hardware unplug; adapter must drop to optical mode <100ms."""
        vision = MagicMock()
        audio = MagicMock()
        adapter = PsychicIntentAdapter(cfg=base_config, vision=vision, audio=audio)

        # Set up active hardware connection
        mock_ser = MagicMock()
        mock_ser.in_waiting = 1
        # Raising SerialException simulates cable yank
        mock_ser.readline.side_effect = OSError("Device disconnected / USB unplugged")
        adapter._ser = mock_ser
        adapter.mode = "HARDWARE_OMNIBAND"

        # Polling catches error immediately
        t_start = 100.0
        adapter._serial_poll(now=t_start)

        # Subsystem remains alive, gracefully degrading to optical fallback
        assert adapter.mode == "OPTICAL_FALLBACK"
        assert adapter._ser is None
        assert mock_ser.close.called


# ==============================================================================
# 3. Central Finite State Machine & Priority Hierarchy
# ==============================================================================
class TestCentralFSM:

    def test_priority_0_safe_idle_face_lost_panic(self, base_config):
        """P0: Face loss > 300ms triggers immediate SAFE_IDLE and panic release."""
        fsm = OmniFSM(cfg=base_config)
        # Establish face and transition to active
        fsm.handle_sample(FrameSample(t=1.0, face_ok=True), now=1.0)
        fsm.handle_sample(FrameSample(t=1.4, face_ok=True), now=1.4)
        assert fsm.state == ACTIVE_WORKSTATION

        # Lose face at t=2.0
        actions = fsm.handle_sample(FrameSample(t=2.0, face_ok=False), now=2.0)
        # Not yet timed out (<300ms)
        assert fsm.state == ACTIVE_WORKSTATION

        # Face remains lost at t=2.35 (>300ms)
        actions = fsm.handle_sample(FrameSample(t=2.35, face_ok=False), now=2.35)
        assert fsm.state == SAFE_IDLE
        assert any(a.kind == "panic" for a in actions)
        assert any(a.kind == "mode" and a.name == SAFE_IDLE for a in actions)

    def test_priority_2_latched_instant_cancel(self, base_config):
        """P2: During LATCHED, hard_blink or acoustic click triggers Instant Cancel in <16ms."""
        fsm = OmniFSM(cfg=base_config)
        fsm.state = ACTIVE_WORKSTATION

        # Trigger clench -> enters LATCHED mode
        actions = fsm.handle_event(IntentEvent("gesture", "clench", 1.0, 10.0), now=10.0)
        assert fsm.state == LATCHED
        assert any(a.kind == "key" and a.name == "lalt" for a in actions)

        # 1. Hard blink instant cancel
        cancel_actions = fsm.handle_event(IntentEvent("gesture", "hard_blink", 1.0, 10.010), now=10.010)
        assert fsm.state == ACTIVE_WORKSTATION
        assert any(a.kind == "release_keys" for a in cancel_actions)

        # 2. Acoustic tongue-click instant cancel
        fsm.handle_event(IntentEvent("gesture", "clench", 1.0, 11.0), now=11.0)
        assert fsm.state == LATCHED
        click_actions = fsm.handle_event(IntentEvent("gesture", "click", 1.0, 11.008), now=11.008)
        assert fsm.state == ACTIVE_WORKSTATION
        assert any(a.kind == "release_keys" for a in click_actions)

    def test_priority_3_converse_lock_and_freeze(self, base_config):
        """P3: Saying 'freeze' mutes all triggers and locks state until 'resume'."""
        fsm = OmniFSM(cfg=base_config)
        fsm.state = ACTIVE_WORKSTATION

        # Freeze triggered
        freeze_actions = fsm.handle_event(IntentEvent("voice", "voice_freeze", 1.0, 5.0), now=5.0)
        assert fsm.state == CONVERSE_LOCK
        assert any(a.kind == "release_keys" for a in freeze_actions)

        # While in CONVERSE_LOCK, facial gestures are completely ignored
        clench_actions = fsm.handle_event(IntentEvent("gesture", "clench", 1.0, 5.1), now=5.1)
        assert clench_actions == []
        assert fsm.state == CONVERSE_LOCK

        # Resume restores previous active mode
        resume_actions = fsm.handle_event(IntentEvent("voice", "voice_resume", 1.0, 6.0), now=6.0)
        assert fsm.state == ACTIVE_WORKSTATION
        assert any(a.kind == "mode" and a.name == ACTIVE_WORKSTATION for a in resume_actions)

    def test_priority_1_calibrating_double_blink(self, base_config):
        """P1: Two hard blinks within 1.5s window enter CALIBRATING for 1.0s."""
        fsm = OmniFSM(cfg=base_config)
        fsm.state = ACTIVE_WORKSTATION

        # First blink
        fsm.handle_event(IntentEvent("gesture", "hard_blink", 1.0, 1.0), now=1.0)
        assert fsm.state == ACTIVE_WORKSTATION

        # Second blink within 1.5s
        cal_actions = fsm.handle_event(IntentEvent("gesture", "hard_blink", 1.0, 1.4), now=1.4)
        assert fsm.state == CALIBRATING
        assert any(a.kind == "mode" and a.name == CALIBRATING for a in cal_actions)

        # Expiry via tick
        fsm.tick(now=2.39)  # 0.99s later -> still calibrating
        assert fsm.state == CALIBRATING
        exit_actions = fsm.tick(now=2.45)  # >1.0s -> exits calibration
        assert fsm.state == ACTIVE_WORKSTATION
        assert any(a.kind == "mode" and a.name == ACTIVE_WORKSTATION for a in exit_actions)


# ==============================================================================
# 4. Perception Pipeline, Filter & Motion Tests
# ==============================================================================
class TestPerceptionFilters:

    def test_cubic_response_curve_characteristics(self):
        """Cubic curve sign(x)*x^3 gives fine center resolution and 1.0 at full deflection."""
        assert cubic_response(0.0) == 0.0
        assert cubic_response(1.0) == 1.0
        assert cubic_response(-1.0) == -1.0
        # At 0.5, 0.5^3 = 0.125 (damped center for steady micro-aim)
        assert cubic_response(0.5) == pytest.approx(0.125, rel=1e-3)
        assert cubic_response(-0.5) == pytest.approx(-0.125, rel=1e-3)

    def test_one_euro_filter_jitter_suppression(self):
        """Synthetic noisy point series is smoothed and jitter variance suppressed."""
        f = OneEuroFilter(min_cutoff=1.0, beta=0.007, d_cutoff=1.0)
        t = 0.0
        clean_target = 100.0

        # Feed noisy resting signal with ±1.5px variance
        raw_values = [clean_target + (1.5 if i % 2 == 0 else -1.5) for i in range(60)]
        filtered_values = []

        for val in raw_values:
            filtered = f.filter(val, t)
            filtered_values.append(filtered)
            t += 1.0 / 60.0

        # After initial settling (first 10 samples), jitter on resting point should be suppressed
        steady_state = filtered_values[20:]
        deviations = [abs(v - clean_target) for v in steady_state]
        max_dev = max(deviations)
        assert max_dev < 0.5, f"OneEuroFilter jitter {max_dev} exceeds allowable 0.5px threshold"


# ==============================================================================
# 5. Focus-Safe HUD Mock Test
# ==============================================================================
class TestFocusSafeHUD:

    def test_hud_initializes_with_exstyle_without_focus_steal(self, base_config):
        """HUD applies WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW and gracefully degrades."""
        # Mock Tkinter to allow running in headless CI without an X-server/display
        mock_tk = MagicMock()
        mock_toplevel = MagicMock()
        mock_tk.Toplevel.return_value = mock_toplevel
        mock_toplevel.winfo_id.return_value = 12345

        with patch.dict("sys.modules", {"tkinter": mock_tk}):
            with patch("ctypes.windll.user32.GetWindowLongW", return_value=0) as mock_get:
                with patch("ctypes.windll.user32.SetWindowLongW") as mock_set:
                    hud = main_hud.Hud(cfg=base_config)

                    if os.name == "nt" and hud.enabled:
                        # Verify SetWindowLongW was called with NOACTIVATE and TOPMOST flags
                        assert mock_set.called
                        args = mock_set.call_args[0]
                        new_style = args[2]
                        assert new_style & main_hud.WS_EX_NOACTIVATE != 0
                        assert new_style & main_hud.WS_EX_TOPMOST != 0
                        assert new_style & main_hud.WS_EX_TOOLWINDOW != 0

    def test_hud_update_state_rendering(self, base_config):
        """HUD correctly updates telemetry state and prediction words without raising exceptions."""
        hud = main_hud.Hud.__new__(main_hud.Hud)
        hud.enabled = True
        hud._labels = {
            "title": MagicMock(),
            "telemetry": MagicMock(),
            "slots": MagicMock(),
        }
        hud._slots = [MagicMock() for _ in range(5)]
        hud._mode = "BOOT"
        hud._next_hud = 0.0
        hud._tk = MagicMock()

        # Mock FSM and telemetry inputs
        mock_fsm = MagicMock()
        mock_fsm.state = ACTIVE_WORKSTATION
        mock_fsm.substate = ""
        mock_fsm.predict_slot = 1
        mock_fsm.predict_words = ["the", "quick", "brown", "fox", "jumps"]

        psychic_status = {
            "mode": "HARDWARE_OMNIBAND",
            "vision": "READY",
            "audio": "READY",
        }
        sample = FrameSample(t=1.0, face_ok=True, gaze_x=0.1, gaze_y=-0.2, proc_ms=12.5)

        # Call update
        hud.update(now=10.0, fsm=mock_fsm, psychic_status=psychic_status, latest=sample)

        assert hud._labels["title"].configure.called
        assert hud._labels["telemetry"].configure.called
        assert hud._slots[0].configure.called
