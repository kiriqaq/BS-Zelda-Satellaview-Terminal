# -*- coding: utf-8 -*-
"""macOS 平台实现"""

import logging
import os
import plistlib
import shlex
import subprocess

from ._base import PlatformBackend

logger = logging.getLogger(__name__)


class MacOSBackend(PlatformBackend):
    """macOS 平台后端 (Apple Silicon / Intel)"""

    def __init__(self):
        self._pygame_joystick_ready = False
        self._init_joystick()

    def _init_joystick(self):
        try:
            import pygame
            from pygame import joystick as pygame_joystick
            if not pygame.get_init():
                pygame.init()
            pygame_joystick.init()
            self._pygame_joystick_ready = True
        except Exception as e:
            logger.warning(f"[手柄] 初始化失败: {e}")
            self._pygame_joystick_ready = False

    # ============================================================
    # 内部辅助
    # ============================================================

    @staticmethod
    def _get_app_pid(app_name_hint):
        try:
            import AppKit
            workspace = AppKit.NSWorkspace.sharedWorkspace()
            for app in workspace.runningApplications():
                name = app.localizedName() or ""
                if app_name_hint.lower() in name.lower():
                    return app.processIdentifier()
        except Exception:
            pass
        return None

    @staticmethod
    def _get_all_windows(include_offscreen=False):
        import Quartz
        option = (Quartz.kCGWindowListOptionAll
                  if include_offscreen
                  else Quartz.kCGWindowListOptionOnScreenOnly)
        return Quartz.CGWindowListCopyWindowInfo(option, Quartz.kCGNullWindowID)

    # ============================================================
    # DPI
    # ============================================================

    def get_dpi_scale(self, win=None):
        return 1.0

    # ============================================================
    # 窗口管理
    # ============================================================

    def find_window(self, title_keyword):
        import Quartz
        try:
            for w in self._get_all_windows():
                name = w.get(Quartz.kCGWindowName, "")
                if title_keyword.lower() in name.lower():
                    owner = w.get(Quartz.kCGWindowOwnerName, "")
                    bounds = w[Quartz.kCGWindowBounds]
                    return {
                        "_native": w,
                        "name": name,
                        "owner": owner,
                        "pid": w.get(Quartz.kCGWindowOwnerPID, 0),
                        "x": int(bounds["X"]),
                        "y": int(bounds["Y"]),
                        "w": int(bounds["Width"]),
                        "h": int(bounds["Height"]),
                        "visible": w.get(Quartz.kCGWindowIsOnscreen, True),
                    }
        except Exception as e:
            logger.warning(f"[窗口] 查找失败: {e}")
        return None

    def get_window_geometry(self, win, dpi_scale=1.0, fullscreen_optimize=False):
        # macOS 标题栏高度估算 (含工具栏约 28pt)
        title_bar = 0 if fullscreen_optimize else 28
        w = win.get("w", 256)
        h = win.get("h", 224)
        x = win.get("x", 0)
        y = win.get("y", 0)

        if w <= 0 or h <= 0:
            return 256, 224, 0, 0

        return w, h - title_bar, x, y + title_bar

    def is_minimized(self, win):
        import Quartz
        target_name = win.get("name", "")
        if not target_name:
            return False
        try:
            for w in self._get_all_windows(include_offscreen=True):
                if w.get(Quartz.kCGWindowName, "") == target_name:
                    return not w.get(Quartz.kCGWindowIsOnscreen, True)
        except Exception:
            pass
        return False

    def restore_window(self, win):
        owner = win.get("owner", "Mesen")
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of process {shlex.quote(owner)} to true'],
                timeout=3
            )
        except Exception as e:
            logger.debug(f"[窗口] 恢复失败: {e}")

    def bring_to_front(self, win):
        owner = win.get("owner", "Mesen")
        try:
            subprocess.run(
                ["open", "-a", owner],
                timeout=5
            )
        except Exception as e:
            logger.debug(f"[窗口] 前置失败: {e}")

    def bring_tk_to_front(self, tk_toplevel):
        try:
            tk_toplevel.attributes("-topmost", True)
            tk_toplevel.update()
        except Exception:
            pass

    # ============================================================
    # 音频控制
    # ============================================================

    @property
    def has_per_app_audio(self):
        return False

    def find_audio_session(self, process_name):
        return None

    def mute_app(self, session, mute):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f"set volume output muted {'true' if mute else 'false'}"],
                timeout=3
            )
            return True, None
        except Exception as e:
            logger.warning(f"[音频] 系统静音失败: {e}")
            return False, None

    # ============================================================
    # 输入轮询
    # ============================================================

    def poll_keyboard(self):
        import Quartz
        for vk in range(0x00, 0x80):
            # 跳过鼠标按钮码 (macOS 鼠标虚拟码通常为 0x0 和特殊事件)
            if Quartz.CGEventSourceKeyState(
                Quartz.kCGEventSourceStateHIDSystemState, vk
            ):
                logger.info(f"[键盘] 检测到键 kVK_{vk:#04x}")
                return True
        return False

    def poll_gamepad(self):
        if not self._pygame_joystick_ready:
            return False
        try:
            import pygame
            from pygame import joystick as pygame_joystick
            pygame.event.pump()
            count = pygame_joystick.get_count()
            for i in range(count):
                js = pygame_joystick.Joystick(i)
                for b in range(js.get_numbuttons()):
                    if js.get_button(b):
                        logger.info(f"[手柄] {i} 号手柄按键 {b}")
                        return True
                for h in range(js.get_numhats()):
                    hx, hy = js.get_hat(h)
                    if hx != 0 or hy != 0:
                        logger.info(f"[手柄] {i} 号手柄 hat {h}: ({hx},{hy})")
                        return True
                for a in range(js.get_numaxes()):
                    if abs(js.get_axis(a)) > 0.5:
                        logger.info(f"[手柄] {i} 号手柄轴 {a}: {js.get_axis(a):.3f}")
                        return True
        except Exception as e:
            logger.debug(f"[手柄] 轮询异常: {e}")
        return False

    # ============================================================
    # 进程与路径
    # ============================================================

    def validate_app_path(self, user_selected_path):
        if not user_selected_path.endswith(".app"):
            # 可能用户已经选了 .app 内部的某文件，向上查找 .app 目录
            return self._resolve_app_executable(user_selected_path)

        # 用户选择了 .app
        info_plist = os.path.join(
            user_selected_path, "Contents", "Info.plist"
        )
        if os.path.exists(info_plist):
            try:
                with open(info_plist, "rb") as f:
                    info = plistlib.load(f)
                exe_name = info.get("CFBundleExecutable", "Mesen")
                exe_path = os.path.join(
                    user_selected_path, "Contents", "MacOS", exe_name
                )
                if os.path.exists(exe_path):
                    return exe_path
            except Exception as e:
                logger.warning(f"[路径] 读取 Info.plist 失败: {e}")

        # 降级：返回 .app 目录路径（后续由 launch_app 处理）
        return user_selected_path

    def _resolve_app_executable(self, path):
        """如果用户选了 .app 内部的文件，向上找到 .app 并提取可执行文件"""
        # 检查路径中是否包含 .app/
        head = path
        while head:
            if head.endswith(".app"):
                return self.validate_app_path(head)
            parent = os.path.dirname(head)
            if parent == head:
                break
            head = parent
        return path

    def get_process_name(self):
        return "Mesen"

    def get_settings_path(self, app_base_dir):
        return os.path.expanduser(
            "~/Library/Application Support/MesenCE/settings.json"
        )

    def get_lua_data_dir(self, app_base_dir):
        return os.path.expanduser(
            "~/Library/Application Support/MesenCE/LuaScriptData/bs"
        )

    def get_satellaview_dir(self):
        return os.path.expanduser(
            "~/Library/Application Support/MesenCE/Satellaview"
        )

    def get_saves_dir(self):
        return os.path.expanduser(
            "~/Library/Application Support/MesenCE/saves"
        )

    def ensure_dirs(self):
        os.makedirs(self.get_lua_data_dir(""), exist_ok=True)
        os.makedirs(self.get_satellaview_dir(), exist_ok=True)
        os.makedirs(self.get_saves_dir(), exist_ok=True)

    def set_app_base_dir(self, mesen_dir):
        pass

    def launch_app(self, exe_path, args):
        if os.path.isdir(exe_path) and exe_path.endswith(".app"):
            return subprocess.Popen([exe_path] + args)
        return subprocess.Popen([exe_path] + args)
