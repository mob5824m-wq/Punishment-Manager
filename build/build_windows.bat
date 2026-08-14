@echo off
REM Build a Windows .exe installer for the Punishment Manager.
REM
REM Requirements (run on Windows):
REM   - Python 3.9+ on PATH
REM   - pip install pyinstaller
REM   - NSIS 3.x in PATH (download from https://nsis.sourceforge.io)
REM   - Optional: a code-signing certificate in the Windows certificate
REM     store. If present, signtool will sign the installer.
REM
REM Output: dist\PunishmentManager-Setup-1.0.0.exe
setlocal enableextensions enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

set "VERSION=1.0.0"
set "INSTALLER_NAME=PunishmentManager-Setup-%VERSION%.exe"

echo ==^> Cleaning previous PyInstaller output (keeps build\ source dir)
if exist dist rmdir /s /q dist
mkdir dist

echo ==^> Building binary with PyInstaller
pyinstaller --noconfirm --clean build\pyinstaller.spec
if errorlevel 1 (
    echo PyInstaller failed.
    exit /b 1
)

if not exist "dist\punishment-manager\punishment-manager.exe" (
    echo ERROR: PyInstaller did not produce the expected binary.
    exit /b 1
)

echo ==^> Building NSIS installer
where makensis >nul 2>nul
if errorlevel 1 (
    echo ERROR: makensis not found. Install NSIS and add it to PATH.
    exit /b 1
)
makensis /DVERSION=%VERSION% /DOUTFILE="dist\%INSTALLER_NAME%" build\windows\installer.nsi
if errorlevel 1 exit /b 1

REM Optional: sign with signtool if a cert is available.
REM signtool sign /fd SHA256 /tr http://timestamp.digicert.com ^
REM     /td SHA256 /a "dist\%INSTALLER_NAME%"

echo.
echo Built: dist\%INSTALLER_NAME%
dir "dist\%INSTALLER_NAME%"

popd
endlocal & exit /b 0
