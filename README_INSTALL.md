# OmniControl — Installation, Building & Permission Guide

OmniControl is a hands-free controller (webcam + mic + switch) for games and desktop apps.
This guide covers building the standalone executable, running hardware/permission diagnostics,
understanding the Windows security boundary (UIPI & anti-cheat), and running as Administrator.

---

## 1. Quick Start & First-Run Diagnostics

### Development Setup (from Source)

```cmd
git clone <repo-url>
cd unbound
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

### First-Run Hardware & Permission Doctor

Always run `--doctor` first. It inspects Python architecture, administrative rights, camera,
microphone, display DPI awareness, SendInput capability, 60 Hz timer jitter, and optional dependencies:

```cmd
omnicontrol --doctor
:: or:
python main.py --doctor
```

Expected output:
- A structured table showing `[PASS]`, `[WARN]`, or `[FAIL]` for each subsystem.
- Exact fix instructions for any warning or failure.
- Exit code 0 if all critical checks pass; non-zero if any check fails.

---

## 2. Building Standalone Windows Executables

OmniControl ships with a PyInstaller specification (`omnicontrol.spec`) and automated build script (`build.bat`)
that produces two distinct distribution targets:

1. **Admin Package (`dist\OmniControl_Admin\OmniControl.exe`)**:
   - Contains an embedded application manifest (`manifest_admin.xml`) with `requestedExecutionLevel="requireAdministrator"`.
   - Windows automatically prompts for UAC elevation upon launch.
   - Recommended for gaming, as it allows injection into elevated and standard applications without UIPI blocking.

2. **Standard Package (`dist\OmniControl\OmniControl.exe`)**:
   - Contains manifest (`manifest_user.xml`) with `requestedExecutionLevel="asInvoker"`.
   - Runs with standard user rights; does not trigger a UAC prompt on launch.
   - Will warn if target games or apps are elevated.

### Build Command

Ensure `pyinstaller` is installed, then run:

```cmd
build.bat
```

Both packages will be compiled into the `dist\` directory:
- `dist\OmniControl_Admin\OmniControl.exe`
- `dist\OmniControl\OmniControl.exe`

---

## 3. Creating the Windows Installer (Inno Setup)

An Inno Setup script (`installer.iss`) compiles the Admin package into a professional Windows setup executable:

1. Download and install [Inno Setup 6+](https://jrsoftware.org/isdl.php).
2. Run `build.bat` to create `dist\OmniControl_Admin`.
3. Open `installer.iss` in Inno Setup Compiler and click **Compile** (or run `iscc installer.iss` from command line).
4. The setup file will be created in `Output\OmniControl_Setup_v0.1.0.exe`.

**Installer Features:**
- Installs to `C:\Program Files\OmniControl` (elevated).
- Creates Start Menu shortcuts for OmniControl and OmniControl Doctor.
- Optional Desktop shortcut.
- Optional "Start with Windows" checkbox (adds an entry to `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, **unchecked/OFF by default**).
- Full clean uninstaller that removes binary files, runtime logs (`%APPDATA%\OmniControl\logs`), and configuration.

---

## 4. Permission Table — What Works, What Cannot, and Why

Windows User Interface Privilege Isolation (UIPI) and kernel security architectures strictly enforce
input boundaries between processes.

| Environment / Target Application | Standard User Mode | Elevated (Run as Admin) | Explanation |
|---|:---:|:---:|---|
| **Standard Desktop Apps** (Notepad, Chrome, Word) | ✅ Works | ✅ Works | Same or lower integrity level; `SendInput` is permitted. |
| **Elevated Apps** (Task Manager, Registry Editor, Admin Terminals) | ❌ Blocked (UIPI) | ✅ Works | Windows UIPI blocks lower-integrity processes from injecting input into higher-integrity windows. Running OmniControl as admin matches integrity. |
| **Single-Player Games** (Standard windowed / borderless) | ✅ Works | ✅ Works | Injected input works normally. |
| **Elevated Single-Player Games** | ❌ Blocked (UIPI) | ✅ Works | Game was launched as admin (e.g., via Steam running elevated). Admin OmniControl required. |
| **Online Games with Kernel Anti-Cheat** (BattlEye, Easy Anti-Cheat, Vanguard, Ricochet) | ❌ Blocked / Ban Risk | ❌ Blocked / Ban Risk | Kernel-mode driver hooks input dispatch or blocks synthetic `SendInput` events below user-space. Running as admin does NOT bypass this and may result in an anti-cheat account ban. |
| **UAC Secure Desktop** (User Account Control elevation prompt) | ❌ Inaccessible | ❌ Inaccessible | Runs on an isolated secure desktop (`Winlogon` desktop session). No regular user-mode process can send input here. |
| **Windows Lock Screen / LogonUI** | ❌ Inaccessible | ❌ Inaccessible | Lock screen and `LogonUI.exe` run in an isolated secure desktop session. OmniControl automatically releases all keys and enters `CONVERSE_LOCK` when session lock is detected. |
| **`uiAccess="true"` Path** | Optional Advanced | Optional Advanced | Requires signing the binary with a trusted code-signing certificate and installing into `C:\Program Files\` or `C:\Windows\System32\`. Enables injection into higher-integrity apps without full admin, but still cannot access the secure desktop or kernel anti-cheat. |

---

## 5. How to Run as Administrator

### Option A: The Packaged Admin Build (Recommended)
Launch `dist\OmniControl_Admin\OmniControl.exe` or the installed Start Menu shortcut. Click **Yes** on the Windows UAC elevation prompt.

### Option B: Automatic Re-elevation Flag
When running via Python or standard executable, pass `--elevate`:
```cmd
python main.py --elevate
```
OmniControl will call `ShellExecuteW(..., "runas", ...)` to request elevation. If accepted, the non-elevated process exits cleanly. If declined, OmniControl logs a clear warning and continues running non-elevated.

### Option C: Elevated Terminal / Right-Click
1. Press `Win + X`, select **Terminal (Admin)** or **Command Prompt (Admin)**.
2. Navigate to your installation directory and run `omnicontrol.exe` or `python main.py`.
3. Alternatively, right-click `OmniControl.exe` and select **Run as administrator**.

---

## 6. Safety Hardening Features

- **Global Panic Keys**:
  - `F12`: Configurable hardware key via `config.json` (`panic_vk: 123`).
  - `Ctrl + Alt + Shift + Q`: Hardware-free chord edge-detected via `GetAsyncKeyState` across all windows.
- **Process Deny-List**:
  - When the foreground window belongs to a process in the deny-list (`consent.exe`, `logonui.exe`, `1password.exe`, `keepass.exe`, `bitwarden.exe`, etc.), OmniControl instantly enters `CONVERSE_LOCK`, releases all keys, and halts injection until focus returns to an allowed app.
- **Rate-Limiter**:
  - Caps synthetic key transitions (default 200/sec). If a producer runs away, OmniControl releases all keys and trips the lock state.
- **Session Lock Detection**:
  - A hidden message window listens for `WM_WTSSESSION_CHANGE` (workstation lock) and `WM_POWERBROADCAST` (sleep/suspend) to release all inputs instantly.
- **Single-Instance Mutex**:
  - Uses `Global\OmniControl_SingleInstance_v1` named mutex to prevent duplicate running copies from contending for input.
- **Crash Safety**:
  - `sys.excepthook` and `threading.excepthook` call `gateway.release_all()` **before** writing the crash traceback to `%APPDATA%\OmniControl\logs\crash.log`.

---

## 7. Known Limits & Honesty Notes

1. **Kernel Anti-Cheat Games**: Games with kernel drivers (BattlEye, EAC, Vanguard) operate at Ring 0 and deliberately filter synthetic `SendInput` events. Running OmniControl as admin does not bypass kernel filters, and online usage can trigger account bans. **Use OmniControl exclusively in single-player and story modes.**
2. **Secure Desktop & Lock Screen**: Windows deliberately isolates `consent.exe` and `logonui.exe`. OmniControl cannot and will not attempt to bypass Windows secure desktop isolation.
3. **`uiAccess="true"` Requirements**: `uiAccess` is a genuine Windows mechanism designed for accessibility tools, but Windows requires the executable to be digitally signed by a trusted root CA certificate and located in a secure directory (`C:\Program Files`). Self-signing requires adding the certificate to the Trusted Root Certification Authorities store.
4. **Verification Scope**:
   - All state machine transitions, rate limiting, deny-list locks, config hot-reloading, doctor checks, elevation decision paths, mutex guards, and crash handlers are **100% automated and unit-tested in pytest**.
   - Direct Win32 API interactions (`user32.SendInput` live hardware dispatch, `GetAsyncKeyState` hardware polling, `WTSRegisterSessionNotification` live session messages) require an interactive Windows desktop session. Structs, flags, and error handling are verified programmatically.

