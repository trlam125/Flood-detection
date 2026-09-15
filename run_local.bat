@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Virtual environment not found. Create it with: py -m venv .venv
  pause
  exit /b 1
)
.venv\Scripts\python.exe run.py
pause
