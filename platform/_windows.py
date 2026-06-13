# -*- coding: utf-8 -*-
"""Windows 平台实现 —— 从 main.py 迁移的 Windows 专属逻辑"""

import ctypes
import logging
import subprocess
from ctypes import windll, wintypes

import pygetwindow as gw

from ._base import PlatformBackend

logger = logging.getLogger(__name__)


class WindowsBackend(PlatformBackend):
    """Windows 平台后端"""

    # Win32 窗口位置结构体
    class _WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("showCmd", ctypes.c_uint),
            ("ptMinPosition", ctypes.c_long * 2),
            ("ptMaxPosition", ctypes.c_long * 2),
            ("rcNormalPosition", ctypes.c_long * 4),
        ]

    # XInput 手柄状态结构体
    class _XinputButtons(ctypes.Structure):
        _fields_ = [("wButtons", ctypes.c_ushort)]

    class _XinputState(ctypes.Structure):
        _fields_ = [
            ("dwPacketNumber", ctypes.c_ulong),
            ("Gamepad", WindowsBackend._XinputButtons),
        ]

    # ============================================================
    # 生命周期
    # ============================================================

    def __init__(self):
        self._app_base_dir = ""
        try:
            from pycaw.pycaw import AudioUtilities
            self._AudioUtilities = AudioUtilities
        except ImportError:
            logger.warning("未检测到 pycaw 库，音频控制不可用")
            self._AudioUtilities = None

    # ============================================================
    # DPI
    # ============================================================

    def get_dpi_scale(self, win=None):
        hwnd = win.get("_hWnd") if win else None
        try:
            if hwnd and hasattr(windll.user32, "GetDpiForWindow"):
                dpi = windll.user32.GetDpiForWindow(hwnd)
                return dpi / 96.0
            if hasattr(windll.user32, "GetDpiForSystem"):
                dpi = windll.user32.GetDpiForSystem()
                return dpi / 96.0
        except Exception as e:
            logger.warning(f"[DPI获取] 现代API调用失败: {e}，尝试传统方法")

        try:
            logpixelsx = 88
            hdc = windll.user32.GetDC(0)
            dpi = windll.gdi32.GetDeviceCaps(hdc, logpixelsx)
            windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
        except Exception as e:
            logger.warning(f"[DPI获取] 传统方法失败: {e}，默认返回 1.0")
            return 1.0

    # ============================================================
    # 窗口管理
    # ============================================================

    def find_window(self, title_keyword):
        wins = [w for w in gw.getWindowsWithTitle(title_keyword) if w.visible]
        if not wins:
            return None
        w = wins[0]
        hwnd = getattr(w, "_hWnd", None)
        return {
            "_gw": w,
            "_hWnd": hwnd,
            "name": w.title,
            "visible": w.visible,
        }

    def get_window_geometry(self, win, dpi_scale=1.0, fullscreen_optimize=False):
        hwnd = win.get("_hWnd")
        if not hwnd:
            return 256, 224, 0, 0

        rect = wintypes.RECT()
        windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        point = wintypes.POINT(0, 0)
        windll.user32.ClientToScreen(hwnd, ctypes.byref(point))

        if fullscreen_optimize:
            offset = 0
        else:
            offset = int(25 * dpi_scale)

        if w <= 0 or h <= 0:
            return 256, 224, 0, 0

        return w, h - offset, point.x, point.y + offset

    def is_minimized(self, win):
        hwnd = win.get("_hWnd")
        if not hwnd:
            return False
        wp = self._WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(self._WINDOWPLACEMENT)
        if windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return wp.showCmd == 2
        return False

    def restore_window(self, win):
        hwnd = win.get("_hWnd")
        if hwnd:
            windll.user32.ShowWindow(hwnd, 9)
            windll.user32.ShowWindow(hwnd, 5)
            windll.user32.SetForegroundWindow(hwnd)

    def bring_to_front(self, win):
        hwnd = win.get("_hWnd")
        if hwnd:
            windll.user32.SetForegroundWindow(hwnd)

    def bring_tk_to_front(self, tk_toplevel):
        try:
            hwnd = tk_toplevel.winfo_id()
            windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    # ============================================================
    # 音频控制
    # ============================================================

    @property
    def has_per_app_audio(self):
        return self._AudioUtilities is not None

    def find_audio_session(self, process_name):
        if self._AudioUtilities is None:
            return None
        try:
            sessions = self._AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.name().lower() == process_name.lower():
                    return session.SimpleAudioVolume
        except Exception as e:
            logger.warning(f"[音频] 查找音频会话失败: {e}")
        return None

    def mute_app(self, session, mute):
        if self._AudioUtilities is None:
            return False, session

        if session:
            try:
                if session.Process and session.Process.status == "running":
                    session.SetMute(1 if mute else 0, None)
                    return True, session
            except (ValueError, AttributeError, OSError):
                logger.warning("[音频] 缓存的音频句柄失效，重新查找...")
                session = None

        # 未缓存或缓存失效，重新查找
        new_session = self.find_audio_session("Mesen.exe")
        if new_session:
            new_session.SetMute(1 if mute else 0, None)
            return True, new_session

        return False, None

    # ============================================================
    # 输入轮询
    # ============================================================

    def poll_keyboard(self):
        for vk_code in range(8, 256):
            if vk_code in (1, 2):
                continue
            if windll.user32.GetAsyncKeyState(vk_code) & 0x8000:
                logger.info(f"[键盘] 检测到按键 VK_{vk_code}")
                return True
        return False

    def poll_gamepad(self):
        state = self._XinputState()
        for user_index in range(4):
            res = windll.xinput1_4.XInputGetState(user_index, ctypes.byref(state))
            if res != 0:
                res = windll.xinput9_1_0.XInputGetState(user_index, ctypes.byref(state))
            if res == 0 and state.Gamepad.wButtons > 0:
                logger.info(f"[手柄] {user_index} 号手柄按键掩码 {state.Gamepad.wButtons}")
                return True
        return False

    # ============================================================
    # 进程与路径
    # ============================================================

    def validate_app_path(self, user_selected_path):
        return user_selected_path

    def get_process_name(self):
        return "Mesen.exe"

    def get_settings_path(self, app_base_dir):
        import os
        return os.path.join(app_base_dir, "settings.json")

    def get_lua_data_dir(self, app_base_dir):
        import os
        return os.path.join(app_base_dir, "LuaScriptData", "bs")

    def get_satellaview_dir(self):
        import os
        return os.path.join(self._app_base_dir, "Satellaview")

    def get_saves_dir(self):
        import os
        return os.path.join(self._app_base_dir, "saves")

    def ensure_dirs(self):
        import os
        os.makedirs(self.get_lua_data_dir(self._app_base_dir), exist_ok=True)
        os.makedirs(self.get_satellaview_dir(), exist_ok=True)
        os.makedirs(self.get_saves_dir(), exist_ok=True)

    def set_app_base_dir(self, mesen_dir):
        self._app_base_dir = mesen_dir

    def launch_app(self, exe_path, args):
        return subprocess.Popen([exe_path] + args)
