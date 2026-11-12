# Stage Runbook — Live Demonstration Script & Chaos Procedures

## 1. Pre-Show Verification (T-60 Minutes)
1. Run system doctor check:
   ```powershell
   python main.py --doctor
   ```
2. Verify native firmware test suite:
   ```powershell
   python tools/run_native_tests.py
   ```
3. Verify test suite health:
   ```powershell
   python -m pytest tests/ -q
   ```
4. Confirm target window allow-list for live arena and Cyberpunk 2077.

---

## 2. Live 3-Act Demonstration Script

### Act 1: The AI Co-Pilot in Action (3D WebGL Arena)
1. Launch Host Engine with Arena profile:
   ```powershell
   python host/omniband_engine.py --simulate --dashboard --mode 0
   ```
2. Open Chrome/Edge at `http://127.0.0.1:8080/arena`.
3. Point out the live **OS Trusted Input Counter** (`event.isTrusted`).
4. Enable `ASSIST: FULL`. Vehicle accelerates and tracks curves smoothly with zero wall hits.
5. Demonstrate **Transparent Vetoes**: Request Nitro entering a hairpin turn $\rightarrow$ Instant HUD Toast: `NITRO VETOED: CORNER AHEAD`.

### Act 2: The Cable Pull & 4-Tier Fallback
1. While vehicle is in full high-speed cruise ($80\text{ km/h}$), simulate a complete USB disconnect using the Chaos Panel or unplugging the band.
2. Observe the **3.0s GRACE state**: The co-pilot keeps the car stable on the road while human keys are released cleanly.
3. At 3.0s expiry, see seamless promotion to **Tier 3 Optical** (Head turning takes over steering).

### Act 3: Cyberpunk 2077 Direct Somatic Control
1. Switch to profile `cyberpunk_2077_direct.json`.
2. Notice HUD updates to `ASSIST: N/A (DIRECT SOMATIC CYBERWARE MODE)`.
3. Drive vehicle through Night City using jaw clenches and temple muscle impulses.

---

## 3. What to Say if X Breaks

| Symptom | Cause | Presenter Action / Talking Point |
| :--- | :--- | :--- |
| **No steering in game** | Focus changed to another app | "Target-window security paused injection to prevent typing in background apps." |
| **Webcam lighting lost** | Camera covered or glare | "Notice the system dropped smoothly to Tier 4 Simulator without crashing." |
| **Telemetry disconnected**| Port collision / tab closed | "Watch the Limp-Home mode safely brake the vehicle rather than accelerating into a wall." |

