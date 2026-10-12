@echo off
cd /d "%~dp0"
title Voice receptionist demo
echo Installing/updating Python packages (first run takes a minute)...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo pip failed. Is Python installed and on PATH?
  pause
  exit /b 1
)
python scripts\run_local_demo.py
pause
