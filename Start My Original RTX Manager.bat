@echo off
setlocal
cd /d "%~dp0"
set "APP=My Original RTX Manager.pyw"

where py.exe >nul 2>&1
if not errorlevel 1 (
    py -3 "%APP%"
    if errorlevel 1 pause
    if errorlevel 1 exit /b 1
    exit /b 0
)

where python.exe >nul 2>&1
if not errorlevel 1 (
    python "%APP%"
    if errorlevel 1 pause
    if errorlevel 1 exit /b 1
    exit /b 0
)

echo.
echo Python 3 with Tkinter was not found.
echo Please install Python from:
echo https://www.python.org/downloads/windows/
echo During setup, enable "Add python.exe to PATH".
echo Then run this file again.
start "" "https://www.python.org/downloads/windows/"
pause
