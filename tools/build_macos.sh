#!/bin/bash
set -e
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== Building BS-Zelda-Satellaview-Terminal for macOS ==="
echo "Project dir: $PROJECT_DIR"

# Clean previous builds
rm -rf dist build

# Lock and install deps (cross-platform + macOS-specific categories)
pipenv lock --categories="packages,macos,dev-packages"
pipenv install --categories="packages,macos,dev-packages"

# Create necessary runtime directories
mkdir -p ~/Library/Application\ Support/MesenCE/LuaScriptData/bs
mkdir -p ~/Library/Application\ Support/MesenCE/Satellaview
mkdir -p ~/Library/Application\ Support/MesenCE/saves
mkdir -p ~/Library/Application\ Support/SatellaviewTerminal

echo "Runtime directories created"

# Build with PyInstaller
pipenv run pyinstaller \
    --name "SatellaviewTerminal" \
    --windowed \
    --add-data "bs.lua:." \
    --add-data "ui:ui" \
    --add-data "platform:platform" \
    --hidden-import mpv \
    --hidden-import pygame \
    --hidden-import PIL \
    --hidden-import PIL.ImageTk \
    --hidden-import Quartz \
    --hidden-import Quartz.CoreGraphics \
    --hidden-import AppKit \
    --hidden-import plistlib \
    --hidden-import tkinter \
    --hidden-import tkinter.filedialog \
    --hidden-import tkinter.messagebox \
    --hidden-import platform \
    --hidden-import platform._macos \
    --hidden-import platform._base \
    --hidden-import platform._windows \
    --collect-all pygame \
    --collect-all python-mpv \
    --osx-bundle-identifier "com.bszelda.satellaview-terminal" \
    --target-architecture arm64 \
    main.py

echo ""
echo "=== Build complete: dist/SatellaviewTerminal.app ==="
echo ""
echo "Deployment notes:"
echo "  1. Install MesenCE for macOS"
echo "  2. brew install mpv sdl2 sdl2_mixer python-tk"
echo "  3. Place bszelda resources and configure base_dir in the UI"
echo "  4. In MesenCE: Script -> Settings -> Script Window -> Restrictions -> Allow IO access"
echo "  5. Lua script (bs.lua) is bundled in the .app, deployed automatically"