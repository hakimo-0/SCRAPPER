#ifndef AppVersion
#define AppVersion "1.0.0"
#endif

[Setup]
AppId={{8F9D9F35-585B-4A4D-A9E0-8A4F5BE775D1}
AppName=SCRAPPER
AppVersion={#AppVersion}
AppPublisher=hakimo-0
DefaultDirName={localappdata}\Programs\SCRAPPER
DefaultGroupName=SCRAPPER
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir=build\installer
OutputBaseFilename=SCRAPPER-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=SCRAPPER

[Files]
Source: "build\stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\SCRAPPER"; Filename: "{app}\start_scrapper.bat"; WorkingDir: "{app}"
Name: "{autodesktop}\SCRAPPER"; Filename: "{app}\start_scrapper.bat"; WorkingDir: "{app}"

[Run]
Filename: "{app}\start_scrapper.bat"; Description: "Start SCRAPPER"; Flags: postinstall nowait skipifsilent
