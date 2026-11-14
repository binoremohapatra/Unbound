@echo off
title Unbound Handsfree AI Platform
cd /d "%~dp0"
python run.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Process exited with code %ERRORLEVEL%
    pause
)
