@echo off
REM One-click launcher: creates the venv on first run, installs deps, starts Setup Manager.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv venv || python -m venv venv || (
        echo.
        echo Python 3 was not found.
        echo Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH", then run this file again.
        pause
        exit /b 1
    )
)
venv\Scripts\python.exe -m pip install -r requirements.txt || (
    echo.
    echo Could not install Setup Manager's own Python packages.
    echo The message from pip is above. Check your internet connection, then run this file again.
    pause
    exit /b 1
)
REM Your projects live in your Projects folder, not in this download.
REM To use a different folder, create run.local.bat next to this file:
REM   set SETUP_MANAGER_SAFE_ROOT=C:\Users\You\SomeFolder
if exist run.local.bat call run.local.bat
echo.
if not defined SETUP_MANAGER_SAFE_ROOT (
    echo Projects folder: your user folder's Projects folder. It is created if it is not there yet.
) else (
    echo Projects folder from run.local.bat: %SETUP_MANAGER_SAFE_ROOT%
)
echo To change it, edit run.local.bat or click "Set as projects root" in the dashboard.
echo.
if not defined SETUP_MANAGER_OLLAMA_MODEL set SETUP_MANAGER_OLLAMA_MODEL=qwen2.5-coder:3b
set SETUP_MANAGER_AUTO_OPEN=true
venv\Scripts\python.exe app.py
pause
