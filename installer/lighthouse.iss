[Setup]
AppName=Lighthouse
AppVersion=1.0.0
AppPublisher=Hamid Taghipourbibalan
AppPublisherURL=https://github.com/Htbibalan/arcitc_lighthouse
AppSupportURL=https://github.com/Htbibalan/arcitc_lighthouse
AppUpdatesURL=https://github.com/Htbibalan/arcitc_lighthouse/releases

DefaultDirName={autopf}\Lighthouse
DefaultGroupName=Lighthouse
DisableProgramGroupPage=yes

OutputDir=installer_output
OutputBaseFilename=Lighthouse_Setup_v1.0.0

SetupIconFile=assets\lighthouse.ico
UninstallDisplayIcon={app}\Lighthouse.exe

Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\Lighthouse.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Lighthouse"; Filename: "{app}\Lighthouse.exe"
Name: "{autodesktop}\Lighthouse"; Filename: "{app}\Lighthouse.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Lighthouse.exe"; Description: "Launch Lighthouse"; Flags: nowait postinstall skipifsilent