; Inno Setup script for Pony Express.
;
; Usage (local):
;   iscc /DAppVersion=0.3.0 installers\windows\pony.iss
;
; The CI passes /DAppVersion and /DDistDir via the build script; you can
; also override DistDir to point at a custom PyInstaller output directory.
;
; Prerequisites: Inno Setup 6+ (https://jrsoftware.org/isinfo.php)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "dist\pony"
#endif

[Setup]
AppId={{8F3A2C1D-4B6E-4F9A-8D2B-1C5E7F0A3B9C}
AppName=Pony Express
AppVersion={#AppVersion}
AppVerName=Pony Express {#AppVersion}
AppPublisher=Juan Jose Garcia-Ripoll
AppPublisherURL=https://github.com/juanjosegarciaripoll/pony
AppSupportURL=https://github.com/juanjosegarciaripoll/pony/issues
AppUpdatesURL=https://github.com/juanjosegarciaripoll/pony/releases
DefaultDirName={autopf}\Pony Express
DefaultGroupName=Pony Express
AllowNoIcons=yes
OutputDir=artifacts
OutputBaseFilename=pony-windows-v{#AppVersion}-setup
SetupIconFile=..\..\icons\pony-express.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add pony to &PATH (recommended)"; GroupDescription: "System integration:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Pony Express Terminal"; Filename: "{app}\pony.exe"
Name: "{group}\Pony Express Documentation"; Filename: "{app}\pony.exe"; Parameters: "docs"
Name: "{group}\Uninstall Pony Express"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\pony.exe"; Parameters: "--version"; Description: "Verify installation"; Flags: nowait postinstall skipifsilent

[Code]
// ---------------------------------------------------------------------------
// PATH manipulation helpers
// ---------------------------------------------------------------------------

function GetCurrentPath: string;
var
  Path: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Path) then
    Path := '';
  Result := Path;
end;

procedure AddToPath(Dir: string);
var
  OldPath: string;
  NewPath: string;
begin
  OldPath := GetCurrentPath;
  if Pos(LowerCase(Dir), LowerCase(OldPath)) > 0 then
    Exit;
  if OldPath = '' then
    NewPath := Dir
  else
    NewPath := OldPath + ';' + Dir;
  RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
end;

procedure RemoveFromPath(Dir: string);
var
  OldPath: string;
  NewPath: string;
  Parts: TStringList;
  I: Integer;
begin
  OldPath := GetCurrentPath;
  Parts := TStringList.Create;
  try
    Parts.Delimiter := ';';
    Parts.StrictDelimiter := True;
    Parts.DelimitedText := OldPath;
    NewPath := '';
    for I := 0 to Parts.Count - 1 do
    begin
      if CompareText(Parts[I], Dir) <> 0 then
      begin
        if NewPath <> '' then
          NewPath := NewPath + ';';
        NewPath := NewPath + Parts[I];
      end;
    end;
    RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
  finally
    Parts.Free;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    if WizardIsTaskSelected('addtopath') then
      AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
