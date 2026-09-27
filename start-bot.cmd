@echo off
REM ---------------------------------------------------------------------------
REM Double-click launcher for local runs (Windows).
REM
REM All the Chinese output comes from start-bot.ps1. This file is deliberately
REM ASCII-only: cmd.exe reads .bat/.cmd using the console code page, so non-ASCII
REM text here would be garbled (or break parsing) depending on the machine locale.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
  echo [X] PowerShell not found. Install Windows PowerShell, then run this again.
  echo     See README.md for the manual steps.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-bot.ps1"

REM Keep the window open so the last lines stay readable after the bot stops.
echo.
pause
endlocal
