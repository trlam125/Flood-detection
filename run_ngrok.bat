@echo off
setlocal
title Flood Detection - ngrok

cd /d "%~dp0"

echo ==========================================
echo   FLOOD DETECTION - NGROK ONLY
echo ==========================================
echo.
echo Make sure your local web is already running at:
echo   http://127.0.0.1:5000
echo.

REM Find ngrok: prefer ngrok.exe in this folder, otherwise use PATH
if exist "ngrok.exe" (
    set "NGROK=%CD%\ngrok.exe"
) else (
    where ngrok >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] ngrok was not found.
        echo.
        echo Install ngrok or place ngrok.exe in:
        echo   %CD%
        echo.
        echo Configure your auth token once with:
        echo   ngrok config add-authtoken YOUR_TOKEN
        echo.
        pause
        exit /b 1
    )
    set "NGROK=ngrok"
)

echo Starting ngrok tunnel to localhost:5000...
echo.
echo Keep this window open while the public link is in use.
echo Press Ctrl+C to stop ngrok.
echo.

"%NGROK%" http 5000

echo.
echo ngrok stopped.
pause
endlocal
