@echo off
REM This launcher MUST stay GBK(936)-encoded with CRLF line endings.
REM cmd.exe reads .cmd via the console code page (GBK/936 here): UTF-8
REM garbles the Chinese paths below, and LF-only endings break parsing.
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%_工具链\启动\启动控制台.ps1"
if errorlevel 1 (
    echo.
    echo 桌面控制台启动失败。请查看上方错误信息后按任意键关闭。
    pause >nul
)
endlocal
