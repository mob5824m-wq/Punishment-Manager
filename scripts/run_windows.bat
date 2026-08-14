@echo off
REM Cross-platform-friendly launcher for Windows.
REM Creates a venv on first run and installs requirements.

setlocal enableextensions enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

if not defined PYTHON set "PYTHON=python"

set "VENV_DIR=.venv"

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment in %VENV_DIR% ...
    "%PYTHON%" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create venv. Is Python 3.9+ installed and on PATH?
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"

echo Installing dependencies ...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

if not exist "data" mkdir data

echo Starting bot ...
python bot.py
set "EXITCODE=%ERRORLEVEL%"

popd
endlocal & exit /b %EXITCODE%
