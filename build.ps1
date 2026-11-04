# Builds dist\CapoCast.exe (single file, no console window) and dist\playlet-cast-extension.zip.
#   .\build.ps1           build
#   .\build.ps1 -Clean    remove previous build output first
param([switch]$Clean)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($Clean) { Remove-Item -Recurse -Force build, dist, *.spec -ErrorAction SilentlyContinue }

# PyInstaller logs to stderr; under Windows PowerShell 5.1 with "Stop" that counts as a failure, so relax it here
$ErrorActionPreference = "Continue"
python -m PyInstaller --noconfirm --onefile --noconsole --name CapoCast `
    --icon assets\youtube.ico --add-data "assets;assets" roku_listen_gui.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$ErrorActionPreference = "Stop"

Compress-Archive -Path extension\* -DestinationPath dist\playlet-cast-extension.zip -Force
Write-Host "Built dist\CapoCast.exe and dist\playlet-cast-extension.zip"
