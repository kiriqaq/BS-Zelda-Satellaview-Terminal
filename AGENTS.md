# AGENTS.md — BS-Zelda-Satellaview-Terminal

## Project Overview
BS 塞尔达传说广播推送终端 — a companion tool for the Chinese-localized BS Zelda that simulates the original Satellaview broadcast experience. 

**Architecture**: Python (Tkinter) main controller → launches MesenCE emulator with Lua script → file-based IPC (Python↔Lua via txt files in LuaScriptData/bs/) for real-time synchronization.

## Directory Structure
```
├── main.py              # Main controller (Tkinter UI, MPV video overlay, clock simulation)
├── bs.lua               # Lua script running inside MesenCE (memory monitoring, input control)
├── platform/            # Platform abstraction layer
│   ├── __init__.py      # Auto-selects backend by sys.platform
│   ├── _base.py         # Abstract base class (PlatformBackend)
│   ├── _windows.py      # Windows: ctypes.windll, pygetwindow, pycaw, XInput
│   └── _macos.py        # macOS: Quartz/CGWindow, osascript, pygame.joystick
├── ui/                  # UI assets (bg_result.png, triforce_on.gif, triforce_off.png)
├── tools/               # Build/packaging scripts
│   ├── build_macos.sh   # PyInstaller build for macOS .app
│   └── build_windows.bat # PyInstaller build for Windows .exe
├── Pipfile              # pipenv dependencies (Aliyun mirror)
├── requirements.txt     # Generated from Pipfile
├── README.md            # User documentation
└── AGENTS.md            # This file
```

## Key Design Patterns

### Platform Abstraction
All platform-specific logic delegates to `self.platform` (instance of WindowsBackend or MacOSBackend):
- Window management: `find_window()`, `get_window_geometry()`, `is_minimized()`
- Audio: `mute_app(session, mute)`, `has_per_app_audio`
- Input: `poll_keyboard()`, `poll_gamepad()`
- Process: `launch_app()`, `get_settings_path()`, `get_lua_data_dir()`

### File IPC Protocol
Python reads/writes signal files in `LuaScriptData/bs/`, Lua script does the same via `emu.getScriptDataFolder()`:
- `chapter_signal.txt` → "1b"/"1g"/"2b"/... (week + gender suffix)
- `load_complete.txt` → "1" when ROM loads
- `result_data.txt` → "DEATH:3|HEART_LOSS:5|RUPEE:100|TRIFORCE:3F|GANON:0"
- `settle_trigger.txt` → "READY"/"DONE"/"IDLE"
- `ganon_spawn.txt` → "1"/"0"

### Resource Paths
Always use `get_resource_path("relative", "path")` which resolves to either:
- `_CURRENT_DIR/relative/path` (dev mode)
- `sys._MEIPASS/relative/path` (PyInstaller bundled)

### Config Persistence
User selections saved to `terminal_config.json` next to main.py via `_save_config()`/`_load_config()`:
- `base_dir`: root directory containing `bszelda/` resources
- `mesen_path`: path to MesenCE executable

## Platform-Specific Paths

| Resource | Windows | macOS |
|----------|---------|-------|
| Settings | `<mesen_dir>/settings.json` | `~/Library/Application Support/MesenCE/settings.json` |
| LuaScriptData | `<mesen_dir>/LuaScriptData/bs/` | `~/Library/Application Support/MesenCE/LuaScriptData/bs/` |
| Satellaview | `<mesen_dir>/Satellaview/` | `~/Library/Application Support/MesenCE/Satellaview/` |
| Saves | `<mesen_dir>/saves/` | `~/Library/Application Support/MesenCE/saves/` |
| App config | `<project_dir>/terminal_config.json` | `~/Library/Application Support/SatellaviewTerminal/terminal_config.json` |

## macOS-Specific Setup
1. Install deps: `brew install mpv sdl2 sdl2_mixer python-tk`
2. Download MesenCE macOS build from GitHub Releases
3. Launch app, go to `Script → Settings → Script Window → Restrictions → Allow access to I/O and OS functions` (must enable manually; cannot be automated via settings.json on macOS NativeAOT build)
4. Also set `BS-X → Use custom date and time → 09:59`

## Common Pitfalls
1. **macOS has no per-app audio API** → `has_per_app_audio` returns False, uses system-wide mute via osascript
2. **CGWindowListCopyWindowInfo returns logical coordinates** → no DPI scaling needed (dpi_scale = 1.0)
3. **pygetwindow is Windows-only** → never import it directly, use platform backend
4. **bs.lua must be launched as CLI argument** to MesenCE → `subprocess.Popen([mesen_exe, rom_path, lua_path])`
5. **macOS .app bundles** → validate_app_path() extracts CFBundleExecutable from Info.plist
6. **Satellaview dir stays relative to mesen_dir** (MesenCE internal expectation), not base_dir
7. **AllowIoOsAccess cannot be set via settings.json on macOS** → user must enable manually in MesenCE UI
8. **macOS NativeAOT build uses `MesenCE` dir** (not `Mesen`) in `~/Library/Application Support/`
9. **bs.lua `package` library is unavailable** in MesenCE sandbox → use `os.getenv("HOME")` for platform detection

## Adding Features Checklist
- [ ] New platform API? Add to _base.py → implement in both _windows.py and _macos.py
- [ ] New resource file? Place in appropriate directory, use get_resource_path()
- [ ] New Lua signal? Add filename to both main.py and bs.lua
- [ ] New UI element? Add to _setup_ui(), handle state in reset_system()
- [ ] New dependency? Add to Pipfile, run `pipenv install && pipenv requirements > requirements.txt`

## Testing
- Windows: Full PC with MesenCE 2.x, Python 3.9+, all audio devices
- macOS: Apple Silicon with MesenCE macOS build, `brew install mpv sdl2`
- Lua script: Load in MesenCE Script Window, verify file writes in LuaScriptData/bs/
