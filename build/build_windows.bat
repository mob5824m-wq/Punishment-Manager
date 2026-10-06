@echo off
REM Build a Windows .exe installer for the Sentinel.
REM
REM Requirements (run on Windows of the architecture you are building for):
REM   - Python 3.9+ on PATH (any install; we re-pip-install pyinstaller
REM     below if the active Python lacks the dev files PyInstaller needs)
REM   - NSIS 3.x in PATH (download from https://nsis.sourceforge.io)
REM   - Optional: a code-signing certificate in the Windows certificate
REM     store. If present, signtool will sign the installer.
REM
REM Output: dist\Sentinel-Setup-<VERSION>.exe        (x64)
REM         dist\Sentinel-Setup-<VERSION>-arm64.exe  (Windows on ARM)
REM
REM One installer per architecture, because PyInstaller cannot cross-compile:
REM run this on an x64 machine for the x64 installer and on a Windows on ARM
REM machine with a native ARM64 Python for the arm64 one. The architecture
REM defaults to the host's via PROCESSOR_ARCHITECTURE; SENTINEL_TARGET_ARCH
REM overrides it, and the build fails if the interpreter (or the .exe it
REM produces) turns out to be the other architecture - an emulated x64 Python
REM on Windows on ARM would otherwise produce a "arm64" installer that is
REM really x64.
REM
REM The version comes from the VERSION file at the project root (same source
REM as build_linux.sh / build_macos.sh / pyinstaller.spec), so a release tag
REM and the installer it produces can't disagree. SENTINEL_VERSION overrides it.
setlocal enableextensions enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." || exit /b 1

if defined SENTINEL_VERSION (
    set "VERSION=%SENTINEL_VERSION%"
) else (
    rem First line of the VERSION file; `for /f` trims the CRLF.
    set "VERSION="
    for /f "usebackq tokens=* delims=" %%v in ("VERSION") do (
        if not defined VERSION set "VERSION=%%v"
    )
    if not defined VERSION set "VERSION=0.0.0+unknown"
)

rem ---------------------------------------------------------------------------
rem Architecture (see scripts\arch.sh for the shell side of the same rules).
rem Normalize to amd64 / arm64; PROCESSOR_ARCHITECTURE is AMD64 on x64 and
rem ARM64 on Windows on ARM.
rem ---------------------------------------------------------------------------
set "ARCH=%SENTINEL_TARGET_ARCH%"
if not defined ARCH set "ARCH=%PROCESSOR_ARCHITECTURE%"
if /i "%ARCH%"=="x86_64" set "ARCH=amd64"
if /i "%ARCH%"=="x64" set "ARCH=amd64"
if /i "%ARCH%"=="amd64" set "ARCH=amd64"
if /i "%ARCH%"=="aarch64" set "ARCH=arm64"
if /i "%ARCH%"=="arm64" set "ARCH=arm64"
if "%ARCH%"=="amd64" goto :arch_ok
if "%ARCH%"=="arm64" goto :arch_ok
echo ERROR: unknown architecture "%ARCH%".
echo        Set SENTINEL_TARGET_ARCH=amd64 or arm64 and try again.
exit /b 1
:arch_ok
rem Keep the whole build - PyInstaller and the arch check in the spec - on the
rem architecture this script resolved.
set "SENTINEL_TARGET_ARCH=%ARCH%"

rem File names carry the architecture when it is not the platform default, so
rem two installers of different architectures can coexist in one release.
set "INSTALLER_NAME=Sentinel-Setup-%VERSION%.exe"
if /i "%ARCH%"=="arm64" set "INSTALLER_NAME=Sentinel-Setup-%VERSION%-arm64.exe"

echo ==^> Building Sentinel %VERSION% for %ARCH%
echo ==^> Cleaning previous PyInstaller output (keeps build\ source dir)
if exist dist rmdir /s /q dist
mkdir dist

echo ==^> Ensuring Python has dev files PyInstaller needs
REM Check for python3.lib next to python3.dll. The slim Python
REM that actions/setup-python installs often lacks this, which
REM causes PyInstaller to fail with "Python library not found".
REM If missing, run the helper script that installs the full Python
REM from Chocolatey or the official MSI.
REM The helper is also the recovery path for a Python of the wrong
REM architecture: `check_arch.py host` reports the interpreter's own
REM architecture, which is AMD64 for an emulated x64 Python running on
REM Windows on ARM and ARM64 only for a native one.
python -c "import sys, os; p=os.path.dirname(sys.executable); lib=os.path.join(p, 'libs', 'python3.lib'); sys.exit(0 if os.path.exists(lib) else 1)" 2>nul
if errorlevel 1 goto :need_install
python scripts\check_arch.py host --expect %ARCH% >nul 2>&1
if errorlevel 1 goto :need_install
echo     Active Python has python3.lib and is %ARCH%; no install needed.
goto :have_install
:need_install
echo     Active Python is unusable for a %ARCH% build; running install helper...
pwsh -NoProfile -ExecutionPolicy Bypass -File .github\scripts\install-windows-deps.ps1 %ARCH% > windows-deps.log 2>&1
if errorlevel 1 (
    echo ERROR: install-windows-deps.ps1 failed. Log:
    type windows-deps.log
    exit /b 1
)
REM After the install, the new Python is somewhere on disk
REM (C:\Python311 or C:\Python311-arm64). The script writes
REM its actual install path to the last line of the log. Read it
REM and prepend to PATH so the rest of this script uses it.
for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Content windows-deps.log -Tail 1"') do set "NEWPY=%%I"
echo     Discovered Python at: %NEWPY%
if exist "%NEWPY%\python.exe" (
    set "PATH=%NEWPY%;%NEWPY%\Scripts;%PATH%"
    echo     Updated PATH to use %NEWPY%
) else (
    echo     WARNING: install log's last line is not a valid Python path.
    echo     Last line was: %NEWPY%
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
    type pyinstaller.log
    exit /b 1
)

if not exist "dist\sentinel\sentinel.exe" (
    echo ERROR: PyInstaller did not produce the expected binary.
    type pyinstaller.log
    exit /b 1
)
echo     PyInstaller output: dist\sentinel\

echo ==^> Verifying the built binary is %ARCH%
REM Reads the PE header of the .exe we just built: building arm64 with an
REM emulated x64 Python would land here as a loud failure instead of a
REM mislabelled installer.
python scripts\check_arch.py file dist\sentinel\sentinel.exe --expect %ARCH%
if errorlevel 1 (
    echo ERROR: dist\sentinel\sentinel.exe is not %ARCH%; not packaging it.
    exit /b 1
)
dir /s /b dist\sentinel 2>nul

echo ==^> Building NSIS installer
REM The Install NSIS step in the workflow sets a MAKENSIS_PATH
REM environment variable to the absolute path of makensis.exe.
REM Use that if set. The variable is set in the PowerShell
REM process and then propagated to the process environment
REM so a subsequent cmd.exe can read it via %MAKENSIS_PATH%.
if defined MAKENSIS_PATH goto :nsis_use_env
goto :nsis_check_default
:nsis_use_env
if exist "%MAKENSIS_PATH%" goto :nsis_use_env_found
echo WARNING: MAKENSIS_PATH is set to '%MAKENSIS_PATH%' but
echo          that file does not exist. Falling back to default.
:nsis_check_default
if exist "C:\nsis-3.10\makensis.exe" goto :nsis_default_310
if exist "C:\nsis-3.09\makensis.exe" goto :nsis_default_309
if exist "C:\nsis-3.08\makensis.exe" goto :nsis_default_308
goto :nsis_not_found
:nsis_default_310
set "MAKENSIS=C:\nsis-3.10\makensis.exe"
goto :nsis_done
:nsis_default_309
set "MAKENSIS=C:\nsis-3.09\makensis.exe"
goto :nsis_done
:nsis_default_308
set "MAKENSIS=C:\nsis-3.08\makensis.exe"
goto :nsis_done
:nsis_use_env_found
set "MAKENSIS=%MAKENSIS_PATH%"
goto :nsis_done
:nsis_not_found
echo ERROR: makensis not found in known locations.
echo Searched:
echo    %%MAKENSIS_PATH%%
echo    C:\nsis-3.10\makensis.exe
echo    C:\nsis-3.09\makensis.exe
echo    C:\nsis-3.08\makensis.exe
echo The Install NSIS step in the workflow should have set
echo MAKENSIS_PATH. Check that step's log.
exit /b 1
:nsis_done
set "MAKENSIS_DIR="
for %%I in ("%MAKENSIS%") do set "MAKENSIS_DIR=%%~dpI"
echo     Found makensis at %MAKENSIS%
set "PATH=%MAKENSIS_DIR%;%PATH%"
REM Use an absolute path for OUTFILE so NSIS writes the installer
REM to <project_root>\dist\ rather than <project_root>\build\windows\dist\
REM /DARCH lets the script label the install (Add/Remove Programs) with the
REM architecture it installs.
makensis /DVERSION=%VERSION% /DARCH=%ARCH% /DOUTFILE="%CD%\dist\%INSTALLER_NAME%" build\windows\installer.nsi
if errorlevel 1 exit /b 1

REM Optional: sign with signtool if a cert is available.
REM signtool sign /fd SHA256 /tr http://timestamp.digicert.com ^
REM     /td SHA256 /a "dist\%INSTALLER_NAME%"

echo.
echo Built: dist\%INSTALLER_NAME% (%ARCH%)
dir "dist\%INSTALLER_NAME%"

popd
endlocal & exit /b 0
