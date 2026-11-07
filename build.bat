@echo off
rem ============================================================================
rem build.bat - Builds both Admin and Normal (asInvoker) PyInstaller packages
rem ============================================================================
setlocal enabledelayedexpansion

echo =======================================================
echo Building OmniControl Executables with PyInstaller
echo =======================================================

where pyinstaller >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller not found. Please run: pip install pyinstaller
    exit /b 1
)

echo.
echo [1/2] Building Admin package (requestedExecutionLevel = requireAdministrator)...
set OMNICONTROL_BUILD_ADMIN=1
pyinstaller --noconfirm --clean omnicontrol.spec
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Admin build failed!
    exit /b %ERRORLEVEL%
)
echo [PASS] dist\OmniControl_Admin generated successfully.

echo.
echo [2/2] Building Normal package (requestedExecutionLevel = asInvoker)...
set OMNICONTROL_BUILD_ADMIN=0
pyinstaller --noconfirm omnicontrol.spec
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Normal build failed!
    exit /b %ERRORLEVEL%
)
echo [PASS] dist\OmniControl generated successfully.

echo.
echo =======================================================
echo Build complete!
echo Output folders:
echo   dist\OmniControl_Admin\OmniControl.exe  (UAC prompt on start)
echo   dist\OmniControl\OmniControl.exe        (Normal user rights)
echo =======================================================

