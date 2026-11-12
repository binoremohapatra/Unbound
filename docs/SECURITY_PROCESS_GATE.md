# Security Documentation: Sink-Level Process Gate (K-03 & K-04)

## Architecture
The `ProcessGate` provides defence-in-depth protection against keystroke leakage (W, A, S, D, Space, Shift, Ctrl) into unauthorized desktop applications.

### Gated Scancodes
`GATED_MOVEMENT_KEYS = {"w", "a", "s", "d", "space", "shift", "lshift", "rshift", "ctrl", "lctrl", "rctrl"}`

### Verification Criteria
A gated scancode is permitted for injection ONLY IF:
1. The active foreground process executable (retrieved via `QueryFullProcessImageNameW`, cached per pid for 250 ms) matches an authorized gaming target (e.g. `cyberpunk2077.exe`, `forzahorizon5.exe`, `gta5.exe`).
2. OR the process is `unbound.exe` AND the window title contains `Cyber-Arena`.

In all other scenarios (Notepad, browser, text editors, unreadable processes, access-denied):
- The press is immediately dropped and logged as `LEAK_BLOCKED`.
- The blocked counter is incremented.
- Any previously held gated keys are released on the very next synchronization flush.

## K-04: Residual Race Condition Documentation
**Analysis of Timing Window**:
Windows input injection via `SendInput()` operates asynchronously with respect to desktop window focus transitions.
1. `ProcessGate.is_key_allowed()` queries `GetForegroundWindow()` and validates the process handle.
2. In the microscopic interval (microseconds) between validation and `SendInput()` hardware queue dispatch, a user could theoretically trigger an OS-level focus switch (e.g., via `Alt+Tab` or a foreground notification popup).
3. **Mitigation**:
   - `Gateway._sync_locked()` evaluates the gate on every single flush tick.
   - When the gate closes, the key is immediately removed from `want_keys`, causing `Gateway` to generate a `key_up` release command within 8 ms.
   - Even in the event of a focus switch occurring in that sub-millisecond gap, at most one isolated key event could occur before immediate release, completely preventing runaway continuous typing (`aaaaaaa...`).

