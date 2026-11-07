# OmniControl — Windows Manual Test Checklist

> Run these tests in order. Each builds on the previous environment assumption.
> All commands from the `unbound/` project root unless noted.

---

## Prerequisites

```
python -m pip install pytest
python -m pytest -q tests/test_safety.py          # must print "44 passed"
```

---

## 1 · Dry-run sanity — no crash, sane logs

```
python main.py --dry-run
```

**Expected:**
- Logs `dry-run backend: no real input will be sent`
- Logs `not running as admin: elevated games will ignore our input (UIPI)` *(normal at this stage)*
- Logs `mapping.py not found: using BasicMapper fallback` *(normal — Dishti's file isn't here yet)*
- No Python traceback
- Ctrl+C exits cleanly; last log line is `summary: {...} overruns=0` or similar
- **No stuck keys** (nothing was ever sent in dry-run, but atexit fires anyway — confirm no error)

---

## 2 · Real input into Notepad — jaw/stick drives keyboard

1. Open **Notepad** (or any text editor) and put it in focus.
2. Run (in a separate terminal, **not** elevated):
   ```
   python main.py
   ```
3. Face the webcam. Move your head **left/right** — cursor/caret should move (W/A/S/D keys).
4. **Open your mouth** past 55 % — the mouse left-button (RT → left-click) holds while your mouth is wide. Release when jaw closes past 32 %.
5. Confirm in the log: `STATE LATCHED (hold:fire)` then `STATE ACTIVE` on jaw release/expiry.

---

## 3 · Face loss — all keys release on the first frame

1. With a latch active (jaw open, `STATE LATCHED` logged):
2. **Cover the camera or step out of frame.**
3. Within one 60 Hz tick (~17 ms after the absent frame): confirm in logs:
   ```
   STATE LATCHED -> SAFE_IDLE (face)
   ```
4. In Notepad, confirm **no key is stuck** (no continuous characters being typed).
5. Return to frame → after **300 ms** of continuous face presence, logs show `STATE ACTIVE`.

> **Note:** A flicker (face disappears then reappears before 300 ms) resets the timer.
> The 300 ms regain dwell is intentional — brief reflections or blinks should not re-arm.

---

## 4 · F12 kill switch — CONVERSE_LOCK

1. With the main process running (and face present):
2. Press **F12**.
3. Logs: `STATE * -> CONVERSE_LOCK (kill)`
4. Move head, open mouth — **no input is injected** while locked.
5. Press **F12 again** — currently this is edge-detected *press*, so it will re-lock (second freeze).
   To unlock: speak `"resume"` (requires Dishti's audio adapter) or simulate a `voice_resume`
   event via replay. In live mode without the audio adapter, restart the process.
6. Confirm that while locked, **release and cancel_all still work**: if a latch had been active
   before locking, it was cancelled at lock-time (logs show `STATE LATCHED -> CONVERSE_LOCK`).

> **Important:** A stuck latch can **never** be trapped in CONVERSE_LOCK — `_lock()` calls
> `_latches.clear()` before emitting `[cancel_all, pause]`, and `cancel_all` / `release` always
> pass `gate()`.

---

## 5 · Alt+Tab while a key is held — foreground watcher releases

1. Hold a latch (`STATE LATCHED` active).
2. **Alt+Tab** out of the game/app to a different window.
3. Within ~250 ms (ForegroundWatcher polls at 4 Hz), logs:
   ```
   STATE LATCHED -> ACTIVE (context_change)
   ```
4. Confirm keys are released in the new foreground window (no spurious keystrokes in a text field).

> **Note:** `ForegroundWatcher` caches the foreground process name and compares by **process name**
> (e.g. `notepad.exe`), not window title, so renaming the window does not defeat it.

---

## 6 · Ctrl+C and console close — no stuck keys

**Test A — Ctrl+C:**
1. Hold a latch.
2. Press Ctrl+C in the terminal.
3. The SIGINT handler calls `release_all("signal 2")` before `SystemExit`.
4. Confirm the target app receives no further input.

**Test B — Close console window:**
1. Hold a latch.
2. Click the ✕ on the terminal window (or use Task Manager to end the console host).
3. `SetConsoleCtrlHandler` fires `release_all("console ctrl")` before the process dies.
4. Confirm no stuck keys.

> **Caution:** On Windows, closing the console window sends `CTRL_CLOSE_EVENT`. The handler has
> only ~5 s before Windows force-kills the process. The release is a single `SendInput` call —
> fast enough.

---

## 7 · UIPI — elevated game, admin fix

1. Run the game **as administrator** (right-click → *Run as administrator*).
2. Run OmniControl **without** elevation:
   ```
   python main.py
   ```
3. Confirm log: `not running as admin: elevated games will ignore our input (UIPI)`
4. Input is silently swallowed by Windows (SendInput returns 0, log shows
   `SendInput sent 0/N (GetLastError=5). Elevated game? Run as admin.`)
5. Now run OmniControl **as administrator** (open terminal with *Run as administrator*):
   ```
   python main.py
   ```
6. The admin warning disappears; input reaches the game.

> **Warning:** Running as admin grants OmniControl elevated privileges over your whole desktop
> session. Only use admin mode for local single-player games. **Never run as admin against online
> games** with anti-cheat (EAC, BattlEye) — they will flag or ban the account.

---

## 8 · GTA V story mode — camera, fire latch, pop cancel

> **Mandatory:** Story mode only, **BORDERLESS WINDOWED** (not fullscreen exclusive, not GTA Online).
> Online anti-cheat will detect synthetic input tools and may issue a ban.

1. Load a story mode session. Stand somewhere safe (no wanted level).
2. Run OmniControl as admin (see §7):
   ```
   python main.py --profile gtav
   ```
   *(Uses BasicMapper fallback if `profiles/gtav.json` or `mapping.py` not yet present — still functional.)*
3. **Head yaw** → right stick X → camera pans left/right. **Head pitch** → camera tilts up/down.
4. **Open jaw wide** → `hold "fire"` → character fires weapon. Hold should persist while jaw is open.
5. **Tongue click (pop)** → logs `STATE LATCHED -> ACTIVE (pop)` → weapon stops firing immediately.
6. **Cover camera** → all input stops within one frame; character stops firing.
7. **F12** → character freezes; speak freely; press F12 to attempt re-lock (or use voice resume).

---

## Replay smoke-test (automated, any OS)

```
python main.py --replay demo.jsonl --dry-run --duration 3
```

**Expected output (order guaranteed):**
```
STATE ACTIVE  -> LATCHED  (hold:fire)      ← jaw_open 0.9
STATE LATCHED -> ACTIVE   (pop)            ← tongue click cancels latch
STATE ACTIVE  -> LATCHED  (hold:fire)      ← jaw_open 0.9 again
STATE LATCHED -> SAFE_IDLE (face)          ← face=0 releases everything
summary: {...} overruns=0
```
fps should be ~60; `ev_ms_p50` will show ~16-17 ms (one tick of quantisation latency).

---

## Honesty Notes

### Latency

| Signal | Realistic cancel latency |
|--------|--------------------------|
| **Face loss** | 1 frame (~17 ms at 60 Hz) from the first absent frame to `cancel_all` |
| **Tongue click (pop)** | ~20–50 ms from the click to mic RMS detection, then 1 tick |
| **Hard blink** | ~230 ms recognition window; Antas's assist decides the threshold |
| **Voice "stop"/"cancel"** | 300–800 ms depending on speech recognition engine latency |

The replay `ev_ms` metric includes **one full tick of quantisation** (up to ~16.7 ms), because
the event timestamp is rebased to the nearest loop boundary. Real live-webcam latency is strictly
less than replay latency and can only be measured with live hardware.

### Platform coverage of tests

| Component | Test method | Verified on |
|-----------|-------------|-------------|
| `INPUT` struct layout (40 bytes, scancodes, EXTENDEDKEY) | `ctypes.sizeof`, flag assertions | Any OS (struct is built even on Linux) |
| `SendInputBackend` | Not unit-tested — requires Win32 `user32.dll` | Windows manual test only |
| `ForegroundWatcher`, `KillSwitch` | Not unit-tested — requires `user32.GetForegroundWindow`, `GetAsyncKeyState` | Windows manual test only |
| `RecordingBackend` (gateway logic, diff, Schmitt, mouse accumulation) | Full unit tests | Any OS |
| `StateMachine` (all 12 safety rule groups) | Full unit tests (44 cases) | Any OS |
| `Watchdog` threads | Real-thread test (`time.sleep(0.3)`) | Any OS |
| `LatencyLog` JSONL | `tmp_path` write + parse | Any OS |
| `ReplaySource` / `Recorder` | Round-trip + face-loss integration | Any OS |

`SetConsoleCtrlHandler` and `timeBeginPeriod(1)` are Windows-only calls wrapped in
`os.name == "nt"` guards. They are not tested on Linux/macOS and produce no-ops there.

