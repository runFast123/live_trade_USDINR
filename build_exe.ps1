# Builds dist\roll_app.exe
#
#   powershell -ExecutionPolicy Bypass -File build_exe.ps1
#
# config.json is NOT bundled. It is read from the folder the .exe sits in, so
# the limit, the tokens and the dry-run flag can be changed without rebuilding.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Running the offline tests first..." -ForegroundColor Cyan
python -m unittest discover -s tests -t .
if ($LASTEXITCODE -ne 0) {
    Write-Host "Tests failed. Not building." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "assets\icon.ico")) {
    Write-Host "Generating the icon..." -ForegroundColor Cyan
    python tools/make_icon.py
}

Write-Host "Building..." -ForegroundColor Cyan
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name roll_app `
    --icon assets/icon.ico `
    --add-data "assets;assets" `
    --collect-all choice_api `
    --hidden-import websocket `
    --hidden-import tkinter `
    --exclude-module PyQt5 --exclude-module PyQt6 `
    --exclude-module PySide2 --exclude-module PySide6 `
    --exclude-module matplotlib --exclude-module scipy `
    --exclude-module IPython --exclude-module zmq `
    --exclude-module notebook --exclude-module jupyter `
    --exclude-module pytest `
    roll_app.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed." -ForegroundColor Red
    exit 1
}

# The GUI build is windowed, so Windows will not attach it to the console that
# launched it and every command line flag prints into the void. The command
# line tools, --probe above all, need a real console build.
Write-Host "Building the console tool..." -ForegroundColor Cyan
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --name roll_cli `
    --icon assets/icon.ico `
    --add-data "assets;assets" `
    --collect-all choice_api `
    --hidden-import websocket `
    --hidden-import tkinter `
    --exclude-module PyQt5 --exclude-module PyQt6 `
    --exclude-module PySide2 --exclude-module PySide6 `
    --exclude-module matplotlib --exclude-module scipy `
    --exclude-module IPython --exclude-module zmq `
    --exclude-module notebook --exclude-module jupyter `
    --exclude-module pytest `
    roll_app.py

if ($LASTEXITCODE -ne 0) {
    Write-Host "Console build failed." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path "dist\config.json")) {
    Copy-Item "config.example.json" "dist\config.json"
    Write-Host "Copied config.example.json to dist\config.json - fill it in." -ForegroundColor Yellow
}
Copy-Item "BRD.md" "dist\BRD.md" -Force

Write-Host ""
Write-Host "Done: dist\roll_app.exe   the window" -ForegroundColor Green
Write-Host "      dist\roll_cli.exe   --probe, --check, --find, --selftest" -ForegroundColor Green
Write-Host "Edit dist\config.json before running. It starts in dry-run mode." -ForegroundColor Green
