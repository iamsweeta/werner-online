@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Neon storage setup
set "PYTHONUTF8=1"
set "TARIFF_PYTHON="

echo Checking Python 3.11 or newer...
if not exist "configure_neon.py" goto missing_project

if exist ".venv\Scripts\python.exe" call :probe ".venv\Scripts\python.exe"
if defined TARIFF_PYTHON goto python_ready
call :probe py -3
if defined TARIFF_PYTHON goto python_ready
call :probe python
if defined TARIFF_PYTHON goto python_ready
call :probe python3
if defined TARIFF_PYTHON goto python_ready
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do call :probe "%%D\python.exe"
if defined TARIFF_PYTHON goto python_ready
for /d %%D in ("%ProgramFiles%\Python3*") do call :probe "%%D\python.exe"
if defined TARIFF_PYTHON goto python_ready

echo.
echo Python 3.11 or newer was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo If offered, enable Add Python to PATH and the Python launcher.
echo After installation, close this window and run this file again.
goto failed

:probe
if defined TARIFF_PYTHON exit /b 0
set "TARIFF_PROBE=%TEMP%\tariff-python-%RANDOM%-%RANDOM%.txt"
%* -c "import sys; assert sys.version_info >= (3,11); print(314159)" >"%TARIFF_PROBE%" 2>nul
set "TARIFF_CHECK="
if exist "%TARIFF_PROBE%" set /p "TARIFF_CHECK="<"%TARIFF_PROBE%"
if exist "%TARIFF_PROBE%" del /q "%TARIFF_PROBE%" >nul 2>nul
if "%TARIFF_CHECK%"=="314159" set "TARIFF_PYTHON=%*"
exit /b 0

:python_ready
echo.
echo Python found:
%TARIFF_PYTHON% --version
%TARIFF_PYTHON% -m pip --version >nul 2>nul
if not errorlevel 1 goto install_requests
%TARIFF_PYTHON% -m ensurepip --upgrade
if errorlevel 1 goto dependency_error

:install_requests
echo.
echo Installing or checking requests...
%TARIFF_PYTHON% -m pip install --disable-pip-version-check requests
if errorlevel 1 goto dependency_error

echo.
echo Starting Neon setup...
echo You need the PostgreSQL connection string and a Neon API key.
echo Passwords are hidden while you paste or type them. Press Enter after pasting.
echo.
%TARIFF_PYTHON% configure_neon.py
if errorlevel 1 goto setup_error
echo.
echo Setup has finished. If successful, render.env is in this folder.
echo Keep render.env private; do not upload it to GitHub.
pause
exit /b 0

:missing_project
echo.
echo configure_neon.py was not found beside this file.
echo Extract the whole application ZIP first.
echo Copy this file into the folder containing configure_neon.py and run again.
goto failed

:dependency_error
echo.
echo Could not install the required Python dependency.
echo Check the error above and your internet connection.
goto failed

:setup_error
echo.
echo Neon setup was not completed. Read the error above.

:failed
echo.
echo You can send a screenshot of this error, without passwords or API keys.
pause
exit /b 1
