Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"
!include "WordFunc.nsh"
Name "FlintDock"
OutFile "publish\FlintDock-1.3.0-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\FlintDock"
InstallDirRegKey HKCU "Software\BedrockServerLauncher" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID zlib
ManifestDPIAware true
VIProductVersion "1.3.0.0"
VIAddVersionKey /LANG=1033 "ProductName" "FlintDock"
VIAddVersionKey /LANG=1033 "FileDescription" "FlintDock Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "1.3.0"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Independent software; third-party notices included."
BrandingText "FlintDock - Ignite your world. Independent software."
!define MUI_ICON "src\branding\flintdock.ico"
!define MUI_UNICON "src\branding\flintdock.ico"
!define MUI_WELCOMEPAGE_TITLE "Welcome to FlintDock"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "Install a self-contained Windows launcher with Python and Tk included.$\r$\n$\r$\nKeep application files separate from your server. On first launch you can connect your own server or import an official Windows Bedrock ZIP.$\r$\n$\r$\nWorlds and customer settings are kept when upgrading or uninstalling. Stop your servers and close the launcher first."
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "customer\LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\FlintDock.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open launcher and set up my server"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\_internal\customer\START-HERE.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Read the getting-started guide"
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "Stop all managed servers and close the launcher first.$\r$\n$\r$\nUninstall removes only application files. Your server folders, worlds, backups, player history and saved setup selection are kept. Unknown files in the app directory are also kept."
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetShellVarContext current
  SetRegView 64
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "This installer requires Windows x64."
    SetErrorLevel 2
    Quit
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "This launcher requires Windows 10 or newer."
    SetErrorLevel 2
    Quit
  ${EndIf}
FunctionEnd

!macro CheckUnlocked PREFIX
Function ${PREFIX}CheckUnlocked
  Push $0
  Push $1
  StrCpy $1 "$INSTDIR\BedrockLauncher.exe"
  Call ${PREFIX}CheckOneFile
  StrCpy $1 "$INSTDIR\BedrockLauncherWorker.exe"
  Call ${PREFIX}CheckOneFile
  StrCpy $1 "$INSTDIR\FlintDock.exe"
  Call ${PREFIX}CheckOneFile
  StrCpy $1 "$INSTDIR\FlintDockWorker.exe"
  Call ${PREFIX}CheckOneFile
  Pop $1
  Pop $0
FunctionEnd
Function ${PREFIX}CheckOneFile
  IfFileExists "$1" 0 done
  System::Call 'kernel32::CreateFileW(w r1, i 0x40000000, i 7, p 0, i 3, i 0, p 0) p.r0'
  ${If} $0 == -1
    IfSilent +2
    MessageBox MB_ICONSTOP "The launcher or its manager is running, or this folder is not writable.$\r$\nStop all managed servers and close the launcher, then try again."
    SetErrorLevel 3
    Abort
  ${EndIf}
  System::Call 'kernel32::CloseHandle(p r0)'
  done:
FunctionEnd
!macroend
!insertmacro CheckUnlocked ""
!insertmacro CheckUnlocked "un."

Function CheckDirectory
  ${GetRoot} "$INSTDIR" $0
  ${If} $INSTDIR == $0
  ${OrIf} $INSTDIR == "$PROFILE"
  ${OrIf} $INSTDIR == "$LOCALAPPDATA"
  ${OrIf} $INSTDIR == "$DOCUMENTS"
    Goto invalid
  ${EndIf}
  ; Refuse app installation inside any existing server tree.
  StrCpy $1 $INSTDIR
  parentloop:
    IfFileExists "$1\server.properties" invalid
    IfFileExists "$1\bedrock_server.exe" invalid
    ${GetParent} "$1" $2
    ${If} $2 == ""
    ${OrIf} $2 == $1
      Goto parentsdone
    ${EndIf}
    StrCpy $1 $2
    Goto parentloop
  parentsdone:
  IfFileExists "$INSTDIR\.launcher-install" owned
  FindFirst $0 $1 "$INSTDIR\*"
  nextentry:
    ${If} $1 == ""
      FindClose $0
      Goto valid
    ${EndIf}
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      FindClose $0
      Goto invalid
    ${EndIf}
    FindNext $0 $1
    Goto nextentry
  owned:
    ReadINIStr $0 "$INSTDIR\.launcher-install" "Application" "ID"
    ${If} $0 != "BDSL-7b3c1e42"
      Goto invalid
    ${EndIf}
    ReadINIStr $0 "$INSTDIR\.launcher-install" "Application" "Version"
    ${VersionCompare} "$0" "1.3.0" $1
    ${If} $1 == 1
      IfSilent +2
      MessageBox MB_ICONSTOP "A newer launcher is installed here. Downgrades are blocked to protect compatibility."
      SetErrorLevel 4
      Abort
    ${EndIf}
  valid:
    Call CheckUnlocked
    Return
  invalid:
    IfSilent +2
    MessageBox MB_ICONSTOP "Choose an empty, dedicated application folder outside your server.$\r$\nFor upgrades, choose the existing launcher installation folder."
    SetErrorLevel 2
    Abort
FunctionEnd

Section "Launcher and Start menu shortcuts (required)" SEC_MAIN
  SectionIn RO
  Call CheckDirectory
  SetOverwrite on
  ; Exact legacy application files only, after ownership and process checks.
  IfFileExists "$INSTDIR\BedrockLauncher.exe" 0 legacydone
  Delete "$INSTDIR\BedrockLauncher.exe"
  Delete "$INSTDIR\BedrockLauncherWorker.exe"
  Delete "$SMPROGRAMS\Bedrock Server Launcher\Bedrock Server Launcher.lnk"
  Delete "$SMPROGRAMS\Bedrock Server Launcher\Server Setup.lnk"
  Delete "$SMPROGRAMS\Bedrock Server Launcher\Getting Started.lnk"
  Delete "$SMPROGRAMS\Bedrock Server Launcher\Uninstall.lnk"
  RMDir "$SMPROGRAMS\Bedrock Server Launcher"
  ; Preserve manually created legacy desktop shortcuts: they may target
  ; another server installation. Customers can replace them with FlintDock.
  legacydone:
  !include "install-files.nsh"
  WriteINIStr "$INSTDIR\.launcher-install" "Application" "ID" "BDSL-7b3c1e42"
  WriteINIStr "$INSTDIR\.launcher-install" "Application" "Version" "1.3.0"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\FlintDock"
  CreateShortcut "$SMPROGRAMS\FlintDock\FlintDock.lnk" "$INSTDIR\FlintDock.exe"
  CreateShortcut "$SMPROGRAMS\FlintDock\Server Setup.lnk" "$INSTDIR\FlintDock.exe" "--setup"
  CreateShortcut "$SMPROGRAMS\FlintDock\Getting Started.lnk" "$INSTDIR\_internal\customer\START-HERE.txt"
  CreateShortcut "$SMPROGRAMS\FlintDock\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\BedrockServerLauncher" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "DisplayName" "FlintDock"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "DisplayVersion" "1.3.0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "DisplayIcon" "$INSTDIR\FlintDock.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher" "NoRepair" 1
SectionEnd

Section /o "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\FlintDock.lnk" "$INSTDIR\FlintDock.exe"
SectionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 64
  ReadINIStr $0 "$INSTDIR\.launcher-install" "Application" "ID"
  ${If} $0 != "BDSL-7b3c1e42"
    IfSilent +2
    MessageBox MB_ICONSTOP "The installation marker is missing. Reinstall to repair before uninstalling. No files were removed."
    SetErrorLevel 2
    Abort
  ${EndIf}
  Call un.CheckUnlocked
FunctionEnd

Section "Uninstall"
  Call un.CheckUnlocked
  ; Generated exact file list; never recursive deletion of app or data folders.
  !include "uninstall-files.nsh"
  Delete "$INSTDIR\.launcher-install"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\FlintDock\FlintDock.lnk"
  Delete "$SMPROGRAMS\FlintDock\Server Setup.lnk"
  Delete "$SMPROGRAMS\FlintDock\Getting Started.lnk"
  Delete "$SMPROGRAMS\FlintDock\Uninstall.lnk"
  RMDir "$SMPROGRAMS\FlintDock"
  Delete "$DESKTOP\FlintDock.lnk"
  ReadRegStr $0 HKCU "Software\BedrockServerLauncher" "InstallLocation"
  ${If} $0 == $INSTDIR
    DeleteRegKey HKCU "Software\BedrockServerLauncher"
    DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BedrockServerLauncher"
  ${EndIf}
SectionEnd
