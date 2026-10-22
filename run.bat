@echo off
REM One-click launcher: creates the venv on first run, installs deps, starts Setup Manager.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv venv || python -m venv venv || (echo Python 3 is required & pause & exit /b 1)
)
venv\Scripts\python.exe -m pip install -q -r requirements.txt || (echo Dependency install failed & pause & exit /b 1)
if not defined SETUP_MANAGER_SAFE_ROOT set SETUP_MANAGER_SAFE_ROOT=C:\dev
set SETUP_MANAGER_AUTO_OPEN=true
venv\Scripts\python.exe app.py
pause
