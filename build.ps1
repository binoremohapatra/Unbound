<#
.SYNOPSIS
    Builds OmniControl on Windows: tests -> icon -> PyInstaller -> smoke test -> installer.

.EXAMPLE
    .\build.ps1                  full build (needs Inno Setup 6.3+ for the installer)
    .\build.ps1 -SkipInstaller   exe folder only: dist\OmniControl\
    .\build.ps1 -SkipTests       when you are in a hurry (do not do this before a demo)

Needs: Python launcher "py" with Python 3.11 installed. Everything else goes into .venv-build.
Output: dist\OmniControl\OmniControl.exe, dist\OmniControl\OmniControl-Admin.exe,
        dist-installer\OmniControl-Setup-<version>.exe
#>
param(
    [string]$PythonVersion = "3.11",
    [switch]$SkipTests,
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step([string]$Message) { Write-Host "`n== $Message" -ForegroundColor Cyan }

function Run([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "'$Exe $($Arguments -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

$match = Select-String -Path "version.py" -Pattern '__version__\s*=\s*"([^"]+)"'
if (-not $match) { throw "could not read __version__ from version.py" }
$version = $match.Matches[0].Groups[1].Value

Step "Python $PythonVersion virtual environment (.venv-build)"
if (-not (Test-Path ".venv-build")) { Run "py" @("-$PythonVersion", "-m", "venv", ".venv-build") }
$py = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
Run $py @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
Run $py @("-m", "pip", "install", "--quiet", "-r", "requirements-build.txt")

if (-not $SkipTests) {
    Step "Tests"
    Run $py @("-m", "pytest", "-q")
}

Step "Icon"
Run $py @("tools\make_icon.py")

Step "PyInstaller (OmniControl $version)"
Run $py @("-m", "PyInstaller", "--noconfirm", "--clean", "omnicontrol.spec")

Step "Checks on the built files"
$normal = "dist\OmniControl\OmniControl.exe"
$admin = "dist\OmniControl\OmniControl-Admin.exe"
foreach ($f in $normal, $admin) { if (-not (Test-Path $f)) { throw "missing $f" } }
# The Admin exe must carry a requireAdministrator manifest; the normal one must not.
function HasAdminManifest([string]$Path) {
    [System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($Path)).Contains("requireAdministrator")
}
if (-not (HasAdminManifest $admin)) { throw "$admin has no requireAdministrator manifest" }
if (HasAdminManifest $normal) { throw "$normal unexpectedly asks for administrator" }
Write-Host "manifests ok (the Admin exe is not started here: it would raise a UAC prompt)"

Step "Smoke test: the built exe must start, import everything, and run the doctor"
$smokeHome = Join-Path $env:TEMP "omnicontrol-smoke"
Remove-Item -Recurse -Force $smokeHome -ErrorAction SilentlyContinue
$env:OMNICONTROL_HOME = $smokeHome   # keep the test out of your real %APPDATA%
$env:OMNICONTROL_NO_GUI = "1"        # never block the build on a message box
$proc = Start-Process -FilePath $normal -ArgumentList "--doctor", "--quick" -Wait -PassThru
$report = Join-Path $smokeHome "logs\doctor-report.txt"
if (-not (Test-Path $report)) { throw "smoke test failed: the exe did not write $report" }
Get-Content $report
# exit 0 = all fine, 1 = the doctor found a problem on THIS machine (not a build failure).
if ($proc.ExitCode -gt 1) { throw "smoke test failed: exit code $($proc.ExitCode)" }
Remove-Item Env:OMNICONTROL_HOME, Env:OMNICONTROL_NO_GUI

if (-not $SkipInstaller) {
    Step "Inno Setup installer"
    $iscc = (Get-Command "iscc" -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        $candidates = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
        )
        foreach ($c in $candidates) { if (Test-Path $c) { $iscc = $c; break } }
    }
    if (-not $iscc) {
        throw "Inno Setup 6.3+ not found (https://jrsoftware.org/isinfo.php). Install it, or run .\build.ps1 -SkipInstaller"
    }
    Run $iscc @("/DAppVersion=$version", "installer.iss")
    Write-Host "Installer: dist-installer\OmniControl-Setup-$version.exe" -ForegroundColor Green
}

Write-Host "`nDone. Run it: dist\OmniControl\OmniControl.exe" -ForegroundColor Green
