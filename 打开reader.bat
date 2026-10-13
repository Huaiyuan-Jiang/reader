@echo off
REM ============================================================
REM  Double-click to open reader (the forced-reading book reader).
REM  This file must stay PURE ASCII: cmd.exe reads .bat files in
REM  the system codepage (GBK), so UTF-8 Chinese here would be
REM  mangled into bogus commands.
REM
REM  pythonw = no black console window. reader allows only one
REM  instance: double-clicking again just brings the open window
REM  to the front.
REM ============================================================
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto console
start "" pythonw -m reader
exit /b 0

:console
REM No pythonw.exe: fall back to python in this window, and keep
REM the window open on errors so the message can be read.
chcp 65001 >nul 2>nul
where python >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH.
    echo Install Python 3.11+ and make sure "python" works in cmd.
    pause
    exit /b 1
)
python -X utf8 -m reader
if errorlevel 1 pause
