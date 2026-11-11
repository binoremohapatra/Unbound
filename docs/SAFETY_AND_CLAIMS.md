# OmniBand V2 — Safety Invariants, Operating Limits & Claims

## 1. Prototype Disclaimer & Life-Safety Boundaries

> [!CAUTION]
> **Research Prototype Only.**
> OmniBand V2 is not a certified medical device and is not validated for clinical intensive care unit (ICU), surgical operating room (OR), firefighter hazmat, or industrial safety deployment. It MUST NOT be used as the sole or primary life-safety alerting mechanism. Any deployment in assistive, industrial, or medical contexts requires independent verification by qualified safety personnel.

---

## 2. False Positive Mitigation

Surface-mounted piezoelectric sensors respond to any mechanical deformation, vibration, or shock. False positives cannot be eliminated by threshold tuning alone without creating unacceptable false-negative rates. The system implements multi-layer structural mitigation:

1. **Chew Guard (§5.4):** Rapid repetitive taps (> 3 taps in 2000 ms) characteristic of chewing or speech lock out multi-tap gesture decoding for 1500 ms and emit `REJECT`.
2. **Impact Burst Veto (§5.3):** Mechanical strikes to the chassis or strap cause simultaneous high-amplitude transients on CH1, CH2, and CH3 within 6 ms. If the event duration is < 40 ms and CH4 does not dominate, it is classified as `NOISE_BURST` and vetoed.
3. **Crosstalk Lookahead & Retro-Cancel (§5.3):** Masseter clenches on CH4 physically deform the temple and forehead, inducing false onsets on CH1 and CH2. CH4 hard transients veto CH1/CH2 onsets within an 80 ms suppression window. In fast modes (M0), retro-cancel signals are emitted retroactively.
4. **Deliberate SOS Architecture:** SOS triggers require three distinct high-amplitude clenches (`TRIPLE`) released within 1500 ms, followed by a countdown confirmation window (1500 ms – 3000 ms) with an inverted visual and audible warning. SOS is completely disabled in gaming (M0), workstation (M1), and daily media (M5) modes.

---

## 3. Host-Side Operating Limitations

1. **Windows UIPI & Elevated Processes:**
   - Under Windows User Interface Privilege Isolation (UIPI), standard-integrity processes calling `SendInput` are silently dropped when the active foreground window is elevated (Run as Administrator), on the secure UAC desktop, or at the Windows Lock Screen. Neither `SendInput` nor `GetLastError` signals failure.
   - The application monitors token elevation and issues dashboard alerts, but control cannot be exerted over elevated windows unless launched via `--elevate`.
2. **Crash & Hard Kill Cleanup:**
   - Standard process termination triggers `Gateway.release_all()` via `atexit`, Windows Console Ctrl handlers (`CTRL_CLOSE_EVENT`, `CTRL_LOGOFF_EVENT`, `CTRL_SHUTDOWN_EVENT`), and POSIX signals.
   - However, an uncatchable `SIGKILL` or Task Manager termination cannot run user code, potentially leaving injected keys in a logically down state until manually tapped on a physical keyboard.
3. **Anti-Cheat Systems:**
   - Kernel-level and ring-3 anti-cheat mechanisms (e.g. Easy Anti-Cheat, BattlEye, Vanguard) may flag or intercept synthetic `SendInput` scancodes. Offline or single-player use is recommended for gaming modes.
4. **Bluetooth SPP Latency Variance:**
   - Bluetooth Classic SPP introduces variable OS scheduling latency (15 ms to 60 ms). For real-time gaming or mission-critical fast paths, wired USB Serial at 115200 or 921600 baud is required.

