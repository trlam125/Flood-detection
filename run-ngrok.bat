@echo off
cd /d "%~dp0"

echo ========================================
echo   RIWA FLOOD MONITOR - NGROK ONLY
echo ========================================
echo.

where ngrok >nul 2>&1
if errorlevel 1 (
    echo [ERROR] ngrok was not found in PATH.
    echo Install ngrok or add ngrok.exe to PATH, then run this file again.
    pause
    exit /b 1
)

echo Creating public tunnel to port 8000...
echo Make sure the web server is already running on port 8000.
echo.

ngrok http 8000

pause
