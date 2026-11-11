<#
.SYNOPSIS
    Compiles and optionally uploads OmniBand V2 firmware using arduino-cli.
.PARAMETER Upload
    If specified, flashes the compiled binary to the designated COM port.
.PARAMETER Port
    Serial COM port for uploading (e.g. COM3).
#>
param (
    [switch]$Upload,
    [string]$Port = "COM3"
)

$ErrorActionPreference = "Stop"
$ArduinoCli = "C:\Program Files\Arduino CLI\arduino-cli.exe"
if (-not (Test-Path $ArduinoCli)) {
    $ArduinoCli = (Get-Command arduino-cli -ErrorAction SilentlyContinue).Source
}

if (-not $ArduinoCli) {
    Write-Error "arduino-cli.exe not found in PATH or standard install directory."
    exit 1
}

$Fqbn = "esp32:esp32:esp32:PartitionScheme=huge_app"
$FirmwareDir = Join-Path $PSScriptRoot "..\firmware\omniband_v2_firmware"

Write-Host "== Compiling OmniBand V2 Firmware ($Fqbn) =="
& $ArduinoCli compile --fqbn $Fqbn --warnings all $FirmwareDir

if ($LASTEXITCODE -ne 0) {
    Write-Error "Compilation failed."
    exit $LASTEXITCODE
}

if ($Upload) {
    Write-Host "== Uploading to $Port =="
    & $ArduinoCli upload -p $Port --fqbn $Fqbn $FirmwareDir
    exit $LASTEXITCODE
}

