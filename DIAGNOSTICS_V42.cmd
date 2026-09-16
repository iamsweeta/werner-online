@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run START_WINDOWS.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" launch.py --diagnostics
if errorlevel 1 pause
