; NSIS installer script for the Punishment Manager.
;
; Build with:
;   makensis /DVERSION=1.0.0 /DOUTFILE="dist\PunishmentManager-Setup-1.0.0.exe" build/windows/installer.nsi
;
; The PyInstaller COLLECT output (dist\punishment-manager\) is wrapped
; into a single Setup.exe that installs to %ProgramFiles64%.

Unicode True
SetCompressor /SOLID lzma
SetDatablockOptimize on
ShowInstDetails hide
ShowUninstDetails hide

!define APPNAME "Punishment Manager"
!define COMPANYNAME "Arena"
!define DESCRIPTION "Discord bot for temporary role-based punishments."

!ifndef VERSION
    !define VERSION "1.0.0"
!endif
!ifndef OUTFILE
    !define OUTFILE "dist\PunishmentManager-Setup-${VERSION}.exe"
!endif

Name "${APPNAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${APPNAME}"
InstallDirRegKey HKLM "Software\${COMPANYNAME}\${APPNAME}" "Install_Dir"
RequestExecutionLevel admin

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SectionIn RO

    ; Write install dir to registry for the uninstaller.
    WriteRegStr HKLM "Software\${COMPANYNAME}\${APPNAME}" "Install_Dir" "$INSTDIR"
    WriteRegStr HKLM "Software\${COMPANYNAME}\${APPNAME}" "Version" "${VERSION}"

    ; Copy the PyInstaller output.
    SetOutPath "$INSTDIR"
    File /r "..\dist\punishment-manager\*"

    ; Start Menu shortcuts.
    CreateDirectory "$SMPROGRAMS\${APPNAME}"
    CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" \
        "$INSTDIR\punishment-manager.exe" "" "$INSTDIR\punishment-manager.exe" 0
    CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" \
        "$INSTDIR\uninstall.exe"

    ; Desktop shortcut.
    CreateShortcut "$DESKTOP\${APPNAME}.lnk" \
        "$INSTDIR\punishment-manager.exe"

    ; Add to PATH for command-line use.
    EnVar::SetHKLM
    EnVar::AddValue "PATH" "$INSTDIR"

    ; Optional: install as a Windows Service via NSSM (if present).
    ;
    ; nsExec::ExecToLog '"$INSTDIR\punishment-manager.exe" --install-service'

    ; Write the uninstaller.
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Register the uninstaller in Add/Remove Programs.
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "DisplayName" "${APPNAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "Publisher" "${COMPANYNAME}"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "NoRepair" 1
SectionEnd

Section "Uninstall"
    ; Remove the install directory.
    RMDir /r "$INSTDIR"

    ; Remove shortcuts.
    Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
    Delete "$SMPROGRAMS\${APPNAME}\Uninstall.lnk"
    RMDir "$SMPROGRAMS\${APPNAME}"
    Delete "$DESKTOP\${APPNAME}.lnk"

    ; Remove from PATH.
    EnVar::SetHKLM
    EnVar::DeleteValue "PATH" "$INSTDIR"

    ; Remove registry keys.
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}"
    DeleteRegKey HKLM "Software\${COMPANYNAME}\${APPNAME}"
SectionEnd
