@echo off
REM This launcher MUST stay GBK(936)-encoded with CRLF line endings.
REM cmd.exe reads .cmd via the console code page (GBK/936 here): UTF-8
REM garbles the Chinese paths below, and LF-only endings break parsing.
setlocal
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%_������\���\�������̨.ps1"
if errorlevel 1 (
    echo.
    echo �������̨���ʧ�ܡ���鿴�Ϸ�������Ϣ��������رա�
    pause >nul
)
endlocal
