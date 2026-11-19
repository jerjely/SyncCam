; Inno Setup script. Built in CI with:
;   iscc /DAppVersion=1.0.0 /DNumericVersion=1.0.0.0 packaging\windows\SyncCam.iss
; Installs per user (no administrator rights needed).

#ifndef AppVersion
  #define AppVersion "0.0.0-test"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif

[Setup]
AppId={{B3F0A6D2-58C4-4E17-9A2B-7D41C9E05F38}
AppName=SyncCam
AppVersion={#AppVersion}
AppVerName=SyncCam {#AppVersion}
AppPublisher=Gergely Szarka
AppPublisherURL=https://github.com/jerjely/SyncCam
AppSupportURL=https://github.com/jerjely/SyncCam/issues
DefaultDirName={autopf}\SyncCam
DefaultGroupName=SyncCam
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\installer_out
OutputBaseFilename=SyncCam-{#AppVersion}-Windows-setup
SetupIconFile=..\..\assets\SyncCam.ico
UninstallDisplayIcon={app}\SyncCam.exe
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
VersionInfoVersion={#NumericVersion}
VersionInfoProductName=SyncCam
VersionInfoDescription=SyncCam setup

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\SyncCam\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\SyncCam"; Filename: "{app}\SyncCam.exe"
Name: "{autodesktop}\SyncCam"; Filename: "{app}\SyncCam.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SyncCam.exe"; Description: "Start SyncCam"; Flags: nowait postinstall skipifsilent
