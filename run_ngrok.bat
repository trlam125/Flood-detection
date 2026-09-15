@echo off
cd /d %~dp0
start "RIWA Flood Web" cmd /k ".venv\Scripts\python.exe run.py"
timeout /t 4 /nobreak >nul
ngrok http 8000
pause
