@echo off
setlocal
cd /d "%~dp0"
title TARIFF COMPARISON - CITY ROUTES - BUILD 53.0

echo =====================================================
echo   TARIFF COMPARISON - BUILD 53.0
echo   SELECT ORIGIN AND DESTINATION IN THE APP
echo   A free local port is selected automatically
echo =====================================================

where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python launcher ^(py^) not found.
  echo Install Python 3.11+ and enable the Python launcher.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  py -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [1/3] Virtual environment already exists.
)

echo [2/3] Installing/checking dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Starting v53.0 on http://127.0.0.1:8423/
".venv\Scripts\python.exe" launch.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo START FAILED. Copy the error text above if you need help.
pause
exit /b 1
