@echo off
REM One-click launcher: creates the venv on first run, installs deps, starts Setup Manager.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3 -m venv venv || python -m venv venv || (echo Python 3 is required & pause & exit /b 1)
)
venv\Scripts\python.exe -m pip install -q -r requirements.txt || (echo Dependency install failed & pause & exit /b 1)
REM Your own folders go in run.local.bat (not shared), for example:
REM   set SETUP_MANAGER_SAFE_ROOT=C:\Users\You\Projects
if exist run.local.bat call run.local.bat
if not defined SETUP_MANAGER_OLLAMA_MODEL set SETUP_MANAGER_OLLAMA_MODEL=qwen2.5-coder:3b
set SETUP_MANAGER_AUTO_OPEN=true
venv\Scripts\python.exe app.py
pause
