@echo off
REM Build a Windows .exe installer for the Punishment Manager.
REM
REM Requirements (run on Windows):
REM   - Python 3.9+ on PATH (any install; we re-pip-install pyinstaller
REM     below if the active Python lacks the dev files PyInstaller needs)
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

echo ==^> Ensuring Python has dev files PyInstaller needs
REM Check for python3.lib next to python3.dll. The slim Python
REM that actions/setup-python installs often lacks this, which
REM causes PyInstaller to fail with "Python library not found".
REM If missing, run the helper script that installs the full Python
REM from Chocolatey or the official MSI.
python -c "import sys, os; p=os.path.dirname(sys.executable); lib=os.path.join(p, '..', 'libs', 'python3.lib'); sys.exit(0 if os.path.exists(lib) else 1)" 2>nul
if errorlevel 1 goto :need_install
echo     Active Python has python3.lib; no install needed.
goto :have_install
:need_install
echo     Active Python lacks python3.lib; running install helper...
pwsh -NoProfile -ExecutionPolicy Bypass -File .github\scripts\install-windows-deps.ps1 > windows-deps.log 2>&1
if errorlevel 1 (
    echo ERROR: install-windows-deps.ps1 failed. Log:
    powershell -NoProfile -Command "Get-Content windows-deps.log"
    exit /b 1
)
:have_install

echo ==^> Installing pyinstaller
python -m pip install --upgrade pip >nul
python -m pip install pyinstaller >nul
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed.
    exit /b 1
)

echo ==^> Building binary with PyInstaller
REM Verbose + log to file so we can see what failed if it fails.
pyinstaller --noconfirm --clean --log-level DEBUG build\pyinstaller.spec > pyinstaller.log 2>&1
if errorlevel 1 (
    echo PyInstaller failed. Last 40 lines of log:
    powershell -NoProfile -Command "Get-Content pyinstaller.log -Tail 40"
    exit /b 1
)

if not exist "dist\punishment-manager\punishment-manager.exe" (
    echo ERROR: PyInstaller did not produce the expected binary.
    powershell -NoProfile -Command "Get-Content pyinstaller.log -Tail 40"
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
