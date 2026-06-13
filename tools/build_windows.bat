@echo off
setlocal
set PROJECT_DIR=%~dp0..
cd /d %PROJECT_DIR%

echo === Building BS-Zelda-Satellaview-Terminal for Windows ===

:: Clean previous builds
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

:: Lock and install deps (cross-platform + Windows-specific categories)
pipenv lock --categories="packages,windows,dev-packages"
pipenv install --categories="packages,windows,dev-packages"

:: Create necessary runtime directories
if not exist "LuaScriptData\bs" mkdir "LuaScriptData\bs"
if not exist "Satellaview" mkdir "Satellaview"
if not exist "saves" mkdir "saves"

echo Runtime directories created

:: Build with PyInstaller
pipenv run pyinstaller ^
    --name "SatellaviewTerminal" ^
    --windowed ^
    --add-data "bs.lua;." ^
    --add-data "ui;ui" ^
    --add-data "platform;platform" ^
    --hidden-import mpv ^
    --hidden-import pygame ^
    --hidden-import PIL ^
    --hidden-import PIL.ImageTk ^
    --hidden-import pygetwindow ^
    --hidden-import pycaw ^
    --hidden-import tkinter ^
    --hidden-import tkinter.filedialog ^
    --hidden-import tkinter.messagebox ^
    --hidden-import platform ^
    --hidden-import platform._windows ^
    --hidden-import platform._base ^
    --hidden-import platform._macos ^
    --collect-all pygame ^
    --collect-all python-mpv ^
    --add-binary "_internal/mpv-1.dll;." ^
    main.py

echo.
echo === Build complete: dist\SatellaviewTerminal.exe ===
echo.
echo Deployment notes:
echo   1. Install MesenCE for Windows
echo   2. Place mpv-1.dll in _internal/ directory
echo   3. Place bszelda resources and configure base_dir in the UI
echo   4. Lua script (bs.lua) is bundled in the .exe, deployed automatically
endlocal