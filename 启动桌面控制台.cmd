@echo off
REM ---------------------------------------------------------------------------
REM QQ_bot desktop console - double-click launcher (Windows).
REM
REM *** THIS FILE MUST STAY PURE ASCII. DO NOT "FIX" ITS ENCODING. ***
REM
REM Why: cmd.exe decodes a .cmd with the **console code page** (936/GBK on a
REM Chinese Windows), not with the encoding the file was saved in. The target of
REM this launcher sits under folders whose names are Chinese, so a Chinese
REM literal written here is decoded against the wrong code page the moment a
REM text tool re-saves the file (editor, export/format pipeline, git checkout).
REM
REM That already happened once, on 2026-10-02: the path below was rewritten as
REM UTF-8 to satisfy the exporter's encoding gate, and the symptom was
REM "double-click, nothing starts" - powershell.exe received a mojibake path and
REM died with "cannot process -File ... invalid characters in a path", while the
REM error text itself was unreadable mojibake.
REM
REM The Chinese relative path therefore travels as ASCII base64 (REL64 below)
REM and is decoded inside PowerShell, which is UTF-8 aware. Any code page, any
REM line ending, any editor: the bytes here stay valid. ASCII is also a subset
REM of UTF-8, so the workspace "text files are UTF-8" rule still holds.
REM
REM To inspect or regenerate the payload (PowerShell):
REM   $b64 = 'X+W3peWFt+mTvlzlkK/liqhc5ZCv5Yqo5o6n5Yi25Y+wLnBzMQ=='
REM   [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))       # what it points at
REM   [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($newRelPath))   # re-encode
REM ---------------------------------------------------------------------------
setlocal
set "ROOT=%~dp0"
set "REL64=X+W3peWFt+mTvlzlkK/liqhc5ZCv5Yqo5o6n5Yi25Y+wLnBzMQ=="

REM "exit $LASTEXITCODE" is required: without it the process exit code of the
REM -Command host is 1 for any value the script's own "exit N" returned (tested).
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$rel=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%REL64%')); & (Join-Path '%ROOT%' $rel); exit $LASTEXITCODE"

if errorlevel 1 (
    echo.
    echo Failed to start the desktop console. Read the messages above, then press any key to close.
    pause >nul
)
endlocal
