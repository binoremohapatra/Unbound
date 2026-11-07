# OmniControl: Windows manual test checklist

Everything below was only verified through fakes or on Linux. The pytest suite proves the logic;
this list proves the real Win32 behaviour. Run it top to bottom on the demo machine, once from
source (steps 1 to 12) and once from the built exe (step 13). Tick each box; write down any
surprise in the "Result" line.

Setup from source (Python 3.11, in the project folder):

```
py -3.11 -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest -q          # must be green on Windows too
```

Keep a second window open on `%LOCALAPPDATA%\OmniControl\logs\omnicontrol.log`
(`Get-Content -Wait` in PowerShell). Every state change and every warning lands there.

**No webcam adapter yet?** Wherever a step says "live session", use demo mode instead:
`python main.py --replay samples\notepad_demo.jsonl --tray`. It shows the tray icon, takes the
single-instance lock, and keeps running after the replay ends until you click Quit. Add
`--speed 0.25` to stretch the 14 s sample to 56 s so you have time to look at the icon.

---

## 1. Doctor on the real machine
`python main.py --doctor`

- [ ] `platform` says Windows 11 (or 10) and the build number.
- [ ] `sendinput` is OK ("accepted 1/1"). **If FAIL:** my zero-move probe may be wrong, or security
      software blocks injection. Tell Binore which; do not ignore it.
- [ ] `webcam privacy` / `microphone privacy` are OK. Now flip *Settings > Privacy > Camera > "Let
      desktop apps access your camera"* off and re-run: it must turn FAIL. (Registry paths were
      written from memory; this confirms them.)
- [ ] `pointer precision` matches *Mouse > Additional mouse settings > Pointer Options*. Turn
      "Enhance pointer precision" on and re-run: WARN. Off: OK.
- [ ] `camera` opens and shows fps; `microphone` lists your device.
- [ ] `loop jitter` p99 is under 2 ms (OK). If not, note the power plan.

Result:

## 2. Notepad: keys reach a normal app (no webcam needed)
Put this in `%APPDATA%\OmniControl\config.json` (start the app once so the file exists):
`"button_map": { "RT": ["key", "a"] }`

Open Notepad, then run `python main.py --replay samples\notepad_demo.jsonl` and click into Notepad
within 5 seconds (the replay starts immediately; the first action is at +6 s).

- [ ] At about +6 s the letter `a` starts repeating.
- [ ] At about +7.2 s (face lost) it **stops instantly**, with no extra letters afterwards.
- [ ] At about +10.1 s it types again and **stops by itself after about 1.8 s**.
- [ ] The log shows `STATE LATCHED`, `STATE SAFE_IDLE`, then back to `ACTIVE`.
- [ ] After the program exits, press `a` on the real keyboard: it types once per press (no stuck key).

Result:

## 3. F12 kill switch while holding
Re-run step 2. Press **F12** while letters are repeating.
- [ ] Typing stops at once. Log: `CONVERSE_LOCK`.
- [ ] Tray menu now says **Resume**; clicking it returns to `ACTIVE`.

Result:

## 4. Alt+Tab while holding
Re-run step 2. Press Alt+Tab to another window while letters are repeating.
- [ ] Typing stops at once (context change cancels everything). Log shows `context_change`.

Result:

## 5. Stopping the program releases keys
Re-run step 2 and stop it during the hold in three ways, one per run:
- [ ] **Ctrl+C** in the console.
- [ ] Close the **console window with the X button** (exercises `SetConsoleCtrlHandler`).
- [ ] **Tray > Quit** in a live session (or demo mode, see the note at the top).
After each: no key is stuck (press `a`, `w`, Shift on the real keyboard).

Result:

## 6. Known limit: hard kill
Start step 2, and while `a` repeats **end the process in Task Manager**.
- [ ] Record what happens. No Python code can run on a hard kill, so a held key may stay down until
      you press it. This is the one failure the in-process safety cannot cover (see "Not covered").

Result:

## 7. Elevated app: UIPI must be detected, not silent
Right-click Notepad > Run as administrator. Run the app **normally** (not elevated) in demo mode
(`--replay samples\notepad_demo.jsonl --tray`, config from step 2) and click into the elevated
Notepad before the first action at +6 s.
- [ ] Log shows `foreground app notepad.exe is elevated and we are not`.
- [ ] Tray tooltip says `INPUT BLOCKED`, icon has the yellow `!` badge, menu offers
      "Restart as administrator (needed for this app)".
- [ ] Nothing is typed in elevated Notepad. (Microsoft documents that SendInput gives no error
      here; this confirms the detection is the only warning.)
- [ ] Click that menu item, accept UAC: a new elevated copy starts, the old one exits, and `a` is
      now typed in elevated Notepad. Declining UAC leaves the old copy running.
- [ ] `python main.py --elevate` does the same from the command line, and does not loop.

Result:

## 8. Single instance
- [ ] Start the app (demo mode is fine), start it again: the second says "already running" and
      exits (a message box when run as the exe).
- [ ] Start normal, then `OmniControl-Admin.exe`: also refused (admin mutex vs normal mutex).
- [ ] Start Admin first, then normal: refused too (this is the ACCESS_DENIED case).
- [ ] Quit, start again: works.

Result:

## 9. Tray
- [ ] Icon appears. Shapes differ per state: play = ACTIVE, dot = LATCHED, bars = CONVERSE_LOCK,
      cross = SAFE_IDLE. In demo mode with `--speed 0.25` you see dot at +24 s and the cross at
      +28.8 s; press F12 or Freeze to see the bars. In a live session, cover your face for the cross.
- [ ] Tooltip text and icon follow state changes within about a quarter of a second.
- [ ] Freeze / Resume, Open settings file, Open logs folder, Run doctor (quick), Quit all work.
- [ ] The main loop does not stutter when the tray menu is open (watch `overruns` in the summary).

Result:

## 10. Config hot reload
While the app runs:
- [ ] Change `button_map` in `config.json` and save: log says `config reloaded`; new binding live,
      and a hold in progress was cancelled first.
- [ ] Save broken JSON: log says unusable, old settings stay, app keeps running.
- [ ] Set `"max_hold_ms": 999999`: clamped to 10000 with a warning.
- [ ] Open the file in old Notepad (adds a BOM) and save: still loads.

Result:

## 11. Safe mode and crash report
- [ ] `python main.py --safe --replay samples\notepad_demo.jsonl`: log says SAFE MODE, `config.json`
      is not read or created, holds are capped at 3 s.
- [ ] Make a crash on purpose: put one line of garbage in a copy of the sample and run
      `python main.py --replay broken.jsonl`. A `crash-*.txt` appears next to the log with the
      traceback and the last log lines.

Result:

## 12. Webcam, live (when Antas's adapter is in)
`python main.py --latency-log lat.jsonl`
- [ ] Face loss (cover the camera) releases within one frame; face must be back for 300 ms to resume.
- [ ] Unplug the camera: after 500 ms the app treats it as face loss.
- [ ] `ev_ms_p95` in `lat.jsonl` is the real event-to-output number for the pitch.

Result:

## 13. The built exe and installer
`.\build.ps1` then:
- [ ] `build.ps1` finished, including its manifest check and smoke test.
- [ ] Double-click `dist\OmniControl\OmniControl.exe`: tray icon, no console window.
- [ ] From PowerShell: `OmniControl.exe --doctor` prints into the terminal; double-clicked
      `OmniControl.exe --doctor` (shortcut) shows a message box instead.
- [ ] `OmniControl-Admin.exe` shows a UAC prompt at every start.
- [ ] Antivirus / SmartScreen: note any warning (unsigned exe).
- [ ] Install `dist-installer\OmniControl-Setup-*.exe` per-user: Start menu entries (normal, Admin,
      Safe Mode, Doctor), optional desktop shortcut and "start at sign-in" both work.
- [ ] Uninstall: files removed, `%APPDATA%\OmniControl\config.json` is kept.

Result:

## 14. Games (single-player, offline only)
- [ ] GTA V story mode, **borderless windowed**: head aims the camera, movement keys work, a hold
      stops when your face leaves the frame.
- [ ] Cyberpunk 2077, same.
- [ ] If a game ignores input: check step 7 first (is the game elevated?), then whether it is in
      exclusive fullscreen.
- Do not test in online modes: games with anti-cheat may ban injected input, and the UAC secure
  desktop and lock screen are unreachable by design.

Result:

---

## Not covered by this app (be honest about it in the demo)
- A hard kill or power loss can leave a key logically down (step 6).
- Native crashes inside a camera or audio library only leave `fatal.log`; they cannot release keys.
- Anti-cheat games, the UAC secure desktop and the lock screen cannot receive injected input.
- Blink recognition needs about 230 ms; tongue click and face loss are the fast interrupts.
