@echo off
REM Launcher for Windows.
REM On first run (or whenever the bot token is missing), this will launch
REM the interactive installer which writes to config.json.

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

REM If config.json has no bot_token and DISCORD_TOKEN isn't set, run the
REM installer so the user can fill in the values interactively.
if not defined DISCORD_TOKEN (
    python -c "import json,sys;cfg=json.load(open('config.json'));sys.exit(0 if (cfg.get('bot_token') or cfg.get('token')) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo No bot token found in config.json. Starting installer ...
        echo.
        python installer.py
        if errorlevel 1 (
            echo Installer exited without saving a config. Aborting.
            exit /b 1
        )
    )
)

echo Starting bot ...
python bot.py
set "EXITCODE=%ERRORLEVEL%"

popd
endlocal & exit /b %EXITCODE%
