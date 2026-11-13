# Cyberpunk 2077 Direct Somatic Cyberware Integration

## 1. Showcase Overview
`Cyberpunk2077.exe` is the installed AAA showcase for direct somatic control. Because Cyberpunk 2077 does not export structured vehicle telemetry, the Co-Pilot assist is explicitly set to:
$$\textbf{ASSIST: N/A (DIRECT SOMATIC CYBERWARE MODE)}$$

All inputs bypass AI stabilization and actuate real game bindings directly via `Gateway.SendInputBackend`.

---

## 2. Configuration & Keybindings

### Game Setup:
- **Display Mode:** Borderless Windowed (required for clean Win32 foreground switching).
- **Target Process:** Allow-listed for `Cyberpunk2077.exe` only. When focus changes to another window, all keys automatically release.

### Somatic Muscle & Optical Map:
| Input Trigger | Action | Cyberpunk 2077 In-Game Function |
| :--- | :--- | :--- |
| **CH1 Hold** | `A` | Steer Vehicle Left / Strafe Left |
| **CH2 Hold** | `D` | Steer Vehicle Right / Strafe Right |
| **CH3 Pulse** | `W` | Accelerate Forward / Cruise Latch |
| **CH4 SINGLE** | `Left Click` / `Space` | Primary Fire / Handbrake Drift |
| **CH4 DOUBLE**| `E` / `R` | Activate Sandevistan Cyberware / Reload |
| **Head Yaw** | Mouse Relative X | Look Left / Look Right in Cockpit |

---

## 3. Simulator Key Non-Collision (TR-07)
Standard simulator keys `C` and `V` interfere with Cyberpunk 2077 default keybindings (`C` = Crouch/Skip Dialog, `V` = Summon Vehicle).

To prevent key leakage, the simulator mapping for direct mode uses:
- **`F7`** $\rightarrow$ CH1
- **`F8`** $\rightarrow$ CH2
- **`F9`** $\rightarrow$ CH3
- **`F10`** $\rightarrow$ CH4

