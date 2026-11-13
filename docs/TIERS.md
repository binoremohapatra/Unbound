# 4-Tier Fallback State Machine & Zero-Stuck-Keys Invariants (TR-01..TR-09)

## 1. The 4 Operational Tiers
To ensure uncompromised accessibility and demo reliability, UNBOUND provides four deterministic fallback tiers:

| Tier | Name | Primary Input Modality | Failover Condition |
| :--- | :--- | :--- | :--- |
| **Tier 1** | `T1_USB` / `T1_BT` | 4-channel OmniBand V2 Piezo EMG | Direct wired USB serial or Bluetooth SPP |
| **Tier 2** | `T2_HYBRID` | Piezo EMG (Clench) + Optical Head Yaw | Both hardware link and webcam active |
| **Tier 3** | `T3_OPTICAL` | Webcam Head Yaw (Steer) + Jaw Open (Actions) | Hardware link silent $\ge 3.0\text{ s}$ |
| **Tier 4** | `T4_SIM` | Keyboard Simulator (`F7-F10` / On-Screen HUD) | No hardware link and no camera |
| **Fail-Safe** | `LIMP_HOME` | Automated Lane-Keeping Braking Stop | All human inputs unavailable |

---

## 2. The 3.0s GRACE Bridge on Cable Pull (TR-03)
When the hardware USB connection is severed (simulated cable pull):
1. **At $t = 150\text{ ms}$ (Link Silence):**
   - The engine immediately calls `Gateway.release_all()` to release human-held key down states.
   - The system enters the **`GRACE` state**.
2. **During GRACE ($0\text{ s} \le t \le 3.0\text{ s}$):**
   - The Co-Pilot maintains lateral lane stabilization and current cruise speed autonomously.
   - The car **never crashes into the wall during an accidental cable disconnect**.
3. **At $t = 3.0\text{ s}$ (GRACE Expiry):**
   - If camera is active $\implies$ Clean promotion to **Tier 3 Optical**.
   - If simulator active $\implies$ Fallback to **Tier 4 Sim**.
   - If all lost $\implies$ Controlled deceleration in **LIMP-HOME**.

---

## 3. Zero Stuck Keys Invariant (TR-05)
Every tier transition executes `Gateway.release_all()`.
The co-pilot re-evaluates and asserts valid steering keys on the next tick ($16.7\text{ ms}$). At zero active inputs, the held key set is guaranteed strictly empty.

