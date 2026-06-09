# -*- coding: utf-8 -*-
import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from tkinter import (Tk, Label, filedialog, StringVar, Checkbutton, Button, Frame, Toplevel, Canvas, messagebox,
                     TclError)

import pygame
from PIL import Image, ImageTk

from platform import PlatformBackend, IS_MACOS, IS_WINDOWS

# 工作目录
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_INTERNAL_DIR = os.path.join(_CURRENT_DIR, "_internal")

# ========== 资源配置 ==========

def _get_config_path():
    """返回配置文件路径（macOS 用 ~/Library/Application Support/，Windows 用脚本目录）"""
    if IS_MACOS:
        app_support = os.path.expanduser("~/Library/Application Support/SatellaviewTerminal")
        os.makedirs(app_support, exist_ok=True)
        return os.path.join(app_support, "terminal_config.json")
    return os.path.join(_CURRENT_DIR, "terminal_config.json")


_CONFIG_FILE = _get_config_path()

def get_resource_path(*segments):
    """获取资源绝对路径，兼容开发模式和 PyInstaller 打包模式"""
    base = getattr(sys, '_MEIPASS', _CURRENT_DIR)
    return os.path.join(base, *segments)


def _load_config():
    """从 JSON 配置文件加载用户上次的选择"""
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_config(data):
    """原子写入 JSON 配置文件"""
    tmp = _CONFIG_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _CONFIG_FILE)
    except OSError:
        pass


def _detect_base_dir():
    """自动检测 base_dir：检查当前目录和常见位置是否存在 bszelda 资源"""
    candidates = [
        _CURRENT_DIR,
        os.path.join(_CURRENT_DIR, ".."),
        os.path.expanduser("~"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/Downloads"),
    ]
    # 检查 /Volumes 下的常见挂载点
    volumes = "/Volumes"
    if os.path.isdir(volumes):
        try:
            for entry in os.listdir(volumes):
                full = os.path.join(volumes, entry)
                if os.path.isdir(full):
                    candidates.append(full)
        except OSError:
            pass
    for d in list(candidates):
        resolved = os.path.abspath(d)
        candidates.append(os.path.expanduser(resolved))
    for d in candidates:
        if os.path.isdir(os.path.join(d, "bszelda")):
            return os.path.abspath(d)
    return _CURRENT_DIR

# ========== 平台初始化 ==========

# 仅 Windows: 启用 DPI 感知
if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

# 仅 Windows: 注入 MPV DLL 搜索路径
if IS_WINDOWS:
    if hasattr(os, "add_dll_directory"):
        for dll_path in [_CURRENT_DIR, _INTERNAL_DIR]:
            if os.path.exists(dll_path):
                try:
                    os.add_dll_directory(dll_path)
                except OSError:
                    pass
    os.environ["PATH"] = (
        _CURRENT_DIR + os.pathsep +
        _INTERNAL_DIR + os.pathsep +
        os.environ.get("PATH", "")
    )

# 引入 MPV 核心库
try:
    import mpv
except ImportError:
    logging.error("未检测到 python-mpv 库，请执行: pip install python-mpv")
    raise

# 核心依赖库加载与初始化
try:
    pygame.mixer.init()
except pygame.error as pg_err:
    logging.error(f"无法初始化音频设备: {pg_err}")

# 全局常量配置
TARGET_WINDOW_TITLE = 'Mesen'  # 匹配 'MesenCE - bs' 和 'Mesen - bs'
SIGNAL_CHECK_INTERVAL = 0.2  # 轮询 Lua 信号文件的时间间隔（秒）
RETRY_DELAY = 0.05  # 文件读取冲突时的重试延迟


def _resolve_file_case(path):
    """大小写不敏感的文件查找，返回实际存在文件的路径，若无匹配则返回原始路径"""
    if os.path.exists(path):
        return path
    dir_name = os.path.dirname(path)
    base_name = os.path.basename(path)
    if not os.path.isdir(dir_name):
        return path
    try:
        for entry in os.listdir(dir_name):
            if entry.lower() == base_name.lower():
                return os.path.join(dir_name, entry)
    except OSError:
        pass
    return path

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()  # 仅保留标准控制台输出
    ]
)


_FONT_FAMILY = "Segoe UI" if IS_WINDOWS else "Helvetica"


class BSXSimulator:
    """
    BS 塞尔达广播模拟终端主类
    """

    def __init__(self):
        # 初始化平台后端与系统环境
        self.platform = PlatformBackend()
        self.dpi_scale = self.platform.get_dpi_scale()
        self.show_debug_ui = False

        # 加载用户配置
        saved = _load_config()
        self.base_dir = saved.get("base_dir", _detect_base_dir())
        self._saved_mesen_path = saved.get("mesen_path", "")

        self.root = Tk()
        self.root.title("Satellaview Terminal 1.4.0")

        window_w = int(340 * self.dpi_scale)
        window_h = int((950 if self.show_debug_ui else 580) * self.dpi_scale)
        self.root.geometry(f"{window_w}x{window_h}")
        self.root.resizable(False, False)

        # 初始化路径变量
        self.mesen_path = ""
        self.mesen_dir = ""
        self.lua_data_dir = ""
        self.bs_sfc_path = ""

        # 初始化状态控制变量
        self.selected_chapter = None
        self.selected_mode = None
        self.timer_running = False
        self.has_triggered_1800 = False
        self._target_window = None
        self._anim_timers = []
        self._mesen_audio_session = None
        self._audio_lock = threading.Lock()
        self._mpv_destroying = False

        # 视频遮罩与 UI 引用容器
        self.overlay = None
        self.canvas = None
        self.video_frame = None
        self.mpv_player = None
        self.bg_image_ref = None
        self.triforce_frames = []
        self.ui_refs = []

        # 播放状态标志
        self.settlement_active = False
        self.is_ending_mode = False
        self.is_waiting_video_mode = False
        self.is_ganon_room_active = False
        self.ganon_mute_timer = None
        self.settlement_audio_locked = False
        self._settlement_sync_active = False
        self._last_geo = ""

        # 绑定 UI 数据变量
        self.time_var = StringVar(value="17:59:00")
        self.status_var = StringVar(value="请定位模拟器的主程序 Mesen.exe ")
        self.death_var = StringVar(value="重新开始的次数: -- 次")
        self.heart_var = StringVar(value="损失的心心数量: -- 个")
        self.rupee_var = StringVar(value="所持的卢比数量: -- 卢比")
        self.triforce_var = StringVar(value="三角力量收集情况:\n△ △ △ △ △ △ △ △")
        self.ganon_var = StringVar(value="？？？？？")
        # 初始化画面比例控制变量，默认开启(true)
        self.keep_aspect_ratio_var = StringVar(value="true")
        self.ntsc_ratio_var = StringVar(value="false")  # 8:7 比例
        self.fullscreen_optimize_var = StringVar(value="false")  # 全屏优化开关
        self._overlay_minimized_by_mesen = False

        self._setup_ui()

        # 注册跨线程绝对安全的事件监听
        self.root.bind("<<VideoEndRouting>>", lambda e: self._safe_trigger_video_routing())
        self.root.bind("<<EndingClose>>", lambda e: self._safe_trigger_ending_close())

    def _get_system_dpi_scale(self, hwnd=None):
        return self.platform.get_dpi_scale(hwnd)

    @staticmethod
    def _load_gif_frames(path, size):
        frames = []
        try:
            img = Image.open(path)
            for i in range(getattr(img, "n_frames", 1)):
                img.seek(i)
                frame = img.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
                frames.append(ImageTk.PhotoImage(frame))
            return frames
        except (OSError, EOFError, AttributeError) as img_err:
            logging.error(f"加载 GIF 帧失败 ({os.path.basename(path)}): {img_err}")
            return []

    def _set_mesen_mute(self, mute=True):
        if not self.platform.has_per_app_audio and not self._mesen_audio_session:
            self.platform.mute_app(None, mute)
            return True

        with self._audio_lock:
            success, new_session = self.platform.mute_app(
                self._mesen_audio_session, mute
            )
            if new_session:
                self._mesen_audio_session = new_session
            return success or not self.platform.has_per_app_audio

    @staticmethod
    def _play_audio(path, volume=1.0, loop=False):
        """安全播放音频的封装方法"""
        if not os.path.exists(path):
            logging.warning(f"[音频] 文件不存在: {path}")
            return False
        try:
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))
            pygame.mixer.music.load(path)
            loops = -1 if loop else 0
            pygame.mixer.music.play(loops=loops)
            logging.info(f"[音频] 开始播放: {os.path.basename(path)}" + (" (循环)" if loop else ""))
            return True
        except Exception as e:
            logging.error(f"[音频] 播放失败 {os.path.basename(path)}: {e}")
            return False

    @staticmethod
    def _stop_audio():
        """安全停止并卸载音频"""
        try:
            if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            pygame.mixer.music.unload()
            logging.info("[音频] 已停止并卸载当前音频")
        except Exception as e:
            logging.warning(f"[音频] 停止时出现异常: {e}")

    def _patch_mesen_settings(self):
        settings_path = self.platform.get_settings_path(self.mesen_dir)
        if not os.path.exists(settings_path):
            fallback_path = os.path.join(self.mesen_dir, "settings.json")
            if os.path.exists(fallback_path):
                settings_path = fallback_path
            else:
                return self._create_default_mesen_settings(settings_path)

        self._apply_mesen_patches(settings_path)

    def _create_default_mesen_settings(self, settings_path):
        config = {
            "Snes": {"BsxUseCustomTime": True, "BsxCustomTime": "09:59:00"},
        }
        try:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            logging.info("[配置] 已创建默认 MesenCE settings.json，请手动开启 Script→Settings→Script Window→Restrictions→Allow IO access")
            return self._apply_mesen_patches(settings_path)
        except OSError as e:
            logging.error(f"[配置] 无法创建默认 settings.json: {e}")

    def _apply_mesen_patches(self, settings_path):
        try:
            with open(settings_path, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)

            # 开启 Lua 脚本系统 IO/OS 访问权限
            if "Debug" in config and "ScriptWindow" in config["Debug"]:
                config["Debug"]["ScriptWindow"]["AllowIoOsAccess"] = True
                logging.info("[配置] 成功开启脚本 IO/OS 访问权限")

            # 修改 BSX 卫星时钟时间
            if "Snes" in config:
                config["Snes"]["BsxUseCustomTime"] = True
                config["Snes"]["BsxCustomTime"] = "09:59:00"
                logging.info("[配置] 成功修改 Bsx 时间")

            # 清除会影响时间轴的快捷键绑定
            if "Preferences" in config and "ShortcutKeys" in config["Preferences"]:
                shortcut_keys_list = config["Preferences"]["ShortcutKeys"]
                if isinstance(shortcut_keys_list, list):
                    modified_shortcuts = 0
                    for shortcut_item in shortcut_keys_list:
                        # 匹配项
                        if shortcut_item.get("Shortcut") in ["FastForward", "Rewind", "IncreaseSpeed", "DecreaseSpeed",
                                                             "MaxSpeed", "Pause", "RunSingleFrame"]:
                            if "KeyCombination" in shortcut_item:
                                shortcut_item["KeyCombination"]["Key1"] = 0
                                modified_shortcuts += 1
                            if "KeyCombination2" in shortcut_item:
                                shortcut_item["KeyCombination2"]["Key1"] = 0
                                modified_shortcuts += 1

                    if modified_shortcuts > 0:
                        logging.info(
                            f"[配置] 成功清除会影响时间轴的快捷键绑定 (共修改 {modified_shortcuts} 项)")

            # 保存修改后的完整 JSON 配置
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)

            logging.info("[配置] settings.json 已成功修改并安全保存")
        except Exception as e:
            logging.error(f"[配置] 自动修改 settings.json 失败: {e}")

    def _fade_volume(self, target_volume, duration=0.8):
        def fade():
            try:
                start_volume = pygame.mixer.music.get_volume()
                steps = 20
                interval = duration / steps
                delta = (target_volume - start_volume) / steps
                for i in range(steps):
                    if self.settlement_audio_locked:
                        return
                    new_vol = start_volume + delta * (i + 1)
                    pygame.mixer.music.set_volume(max(0.0, min(1.0, new_vol)))
                    time.sleep(interval)
                if not self.settlement_audio_locked:
                    pygame.mixer.music.set_volume(target_volume)
            except Exception as e:
                logging.error(f"音量渐变执行失败: {e}")

        threading.Thread(target=fade, daemon=True, name="AudioFadeThread").start()

    def _get_client_geometry(self, win, fullscreen_optimize=False):
        return self.platform.get_window_geometry(win, self.dpi_scale, fullscreen_optimize)

    def _setup_ui(self):
        # 字号转为绝对物理像素，与窗口框架 1:1 纯线性对齐
        dynamic_ui_font = (_FONT_FAMILY, -int(26 * self.dpi_scale), "bold")
        dynamic_monitor_font = (_FONT_FAMILY, -int(14 * self.dpi_scale), "bold")
        dynamic_tf_font = (_FONT_FAMILY, -int(16 * self.dpi_scale), "bold")

        # 界面间距随 DPI 实时调整物理高度
        pad_5 = int(5 * self.dpi_scale)
        pad_10 = int(10 * self.dpi_scale)
        # pad_15 = int(15 * self.dpi_scale)
        pad_20 = int(20 * self.dpi_scale)

        time_frame = Frame(self.root, pady=pad_10)
        time_frame.pack()
        Label(time_frame, text="虚拟卫星时钟", font=dynamic_ui_font).pack(side="left")
        self.time_display = Label(time_frame, textvariable=self.time_var, font=dynamic_ui_font, fg="#e74c3c",
                                  padx=pad_10)
        self.time_display.pack(side="left")

        # wraplength 随 DPI 缩放，去掉高度死值，改用自动换行支撑，防止字变大后被拦腰截断
        self.status_label = Label(self.root, textvariable=self.status_var, font=dynamic_tf_font, fg="#c0392b",
                                  wraplength=int(350 * self.dpi_scale), height=2, justify="center")
        self.status_label.pack(pady=pad_10)

        # 模式选择框架（表模式 / 里模式）
        self.mode_frame = Frame(self.root, pady=pad_10)
        self.mode_frame.pack()

        self.btn_mode_m1 = Button(self.mode_frame, text="表模式", width=10, height=2, state="disabled",  # 改为 disabled
                                  command=lambda: self.select_mode("m1"))
        self.btn_mode_m1.pack(side="left", padx=pad_10)

        self.btn_mode_m2 = Button(self.mode_frame, text="里模式", width=10, height=2, state="disabled",  # 改为 disabled
                                  command=lambda: self.select_mode("m2"))
        self.btn_mode_m2.pack(side="left", padx=pad_10)

        self.ch_frame = Frame(self.root, pady=pad_10)
        self.ch_frame.pack()
        self.chapter_buttons = []
        for i in range(1, 5):
            btn = Button(self.ch_frame, text=f"第 {i} 周", state="disabled", width=6,
                         command=lambda ch=i: self.prepare_chapter(ch))
            btn.pack(side="left", padx=pad_10)
            self.chapter_buttons.append(btn)

        self.btn_select = Button(self.root, text="第 1 步：选择模拟器根目录的 Mesen.exe ", command=self.select_mesen,
                                 width=35, height=2)
        self.btn_select.pack(pady=pad_20)

        self.btn_base_dir = Button(self.root, text="自定义资源目录（可选）", command=self.select_base_dir,
                                   width=35, height=1)
        self.btn_base_dir.pack(pady=pad_5)

        self.btn_stop = Button(self.root, text="重置状态（选择其他周）", command=self.reset_system, width=35, height=1,
                               state="disabled")
        self.btn_stop.pack(pady=pad_5)

        self.btn_delete_save = Button(self.root, text="删除存档（需要关闭模拟器后使用）", command=self.delete_bios_save,
                                      width=35, height=1,
                                      state="disabled", fg="#c0392b")
        self.btn_delete_save.pack(pady=pad_5)

        # 画面比例选择框架
        ratio_frame = Frame(self.root, pady=pad_10)
        ratio_frame.pack()

        self.chk_aspect = Checkbutton(
            ratio_frame,
            text="强制 4：3 画面比例",
            variable=self.keep_aspect_ratio_var,
            onvalue="true",
            offvalue="false",
            activebackground=self.root.cget("bg"),
            command=self._on_ratio_changed
        )
        self.chk_aspect.pack(side="left", padx=pad_5)

        self.chk_ntsc = Checkbutton(
            ratio_frame,
            text="强制 NTSC 画面比例",
            variable=self.ntsc_ratio_var,
            onvalue="true",
            offvalue="false",
            activebackground=self.root.cget("bg"),
            command=self._on_ntsc_changed
        )
        self.chk_ntsc.pack(side="left", padx=pad_5)

        # 全屏优化勾选框
        self.chk_fullscreen = Checkbutton(
            self.root,
            text="F11 全屏优化（不全屏不需要）",
            variable=self.fullscreen_optimize_var,
            onvalue="true",
            offvalue="false",
            activebackground=self.root.cget("bg")
        )
        self.chk_fullscreen.pack(pady=pad_10)

        # 调试监视中心
        if self.show_debug_ui:
            monitor_section = Frame(self.root, pady=pad_10, padx=pad_20, relief="groove", borderwidth=2)
            monitor_section.pack(fill="x", padx=pad_20, pady=pad_10)
            Label(monitor_section, text="[ 结算数据监视中心 ]", font=dynamic_ui_font, fg="#2980b9").pack(
                pady=(0, pad_5))
            result_subframe = Frame(monitor_section)
            result_subframe.pack(fill="x")
            Label(result_subframe, textvariable=self.ganon_var, font=dynamic_monitor_font, fg="#e74c3c").pack()

            # 三角力量数据监控自动换行边界物理缩放
            Label(result_subframe, textvariable=self.triforce_var, font=dynamic_tf_font, fg="#f39c12",
                  wraplength=int(300 * self.dpi_scale)).pack()

            Label(result_subframe, textvariable=self.death_var, font=dynamic_monitor_font).pack(anchor="w")
            Label(result_subframe, textvariable=self.heart_var, font=dynamic_monitor_font).pack(anchor="w")
            Label(result_subframe, textvariable=self.rupee_var, font=dynamic_monitor_font).pack(anchor="w")
            self.btn_test = Button(monitor_section, text="立即测试数据", command=self.update_settlement_display,
                                   bg="#ecf0f1", height=1)
            self.btn_test.pack(pady=pad_5, fill="x")

    def _on_ratio_changed(self):
        """4:3 勾选时，自动取消 8:7 的勾选"""
        if self.keep_aspect_ratio_var.get() == "true":
            self.ntsc_ratio_var.set("false")

    def _on_ntsc_changed(self):
        """8:7 勾选时，自动取消 4:3 的勾选"""
        if self.ntsc_ratio_var.get() == "true":
            self.keep_aspect_ratio_var.set("false")

    def _get_target_ratio(self):
        """获取当前选择的目标比例 (width/height)"""
        if self.ntsc_ratio_var.get() == "true":
            # NTSC 8:7 勾选时，返回 4:3（因为素材是为 4:3 做的）
            return 4 / 3
        else:
            # 否则（包括勾选 4:3 或都不勾选），返回 8:7
            return 8 / 7

    def select_base_dir(self):
        """用户自定义资源根目录（bszelda 所在目录的父级）"""
        path = filedialog.askdirectory(title="请选择 bszelda 资源所在的根目录（包含 bszelda 文件夹的那一层）")
        if path:
            path = os.path.abspath(path)
            # 规范化：若用户选了 bszelda 目录本身，取其父目录
            if os.path.basename(path) == "bszelda":
                path = os.path.dirname(path)
                logging.info(f"[base_dir] 自动规范化到 bszelda 父目录: {path}")
            self.base_dir = path
            self._save_user_config()
            self._update_base_dir_ui()
            if self.mesen_dir:
                self.bs_sfc_path = os.path.join(self.base_dir, "bszelda", "bs.sfc")

    def _save_user_config(self):
        _save_config({
            "base_dir": self.base_dir,
            "mesen_path": self._saved_mesen_path,
        })

    def _update_base_dir_ui(self):
        short = self.base_dir
        if len(short) > 40:
            short = "..." + short[-37:]
        self.btn_base_dir.config(
            text=f"资源目录: {short}",
            fg="#27ae60"
        )

    def select_mesen(self):
        if IS_MACOS:
            # macOS NSOpenPanel 不支持按 .app 扩展名过滤 (需 UTI)
            # askopenfilename 将 .app bundle 视为不透明文件，因此可选中
            path = filedialog.askopenfilename(
                title="请选择 Mesen.app",
                filetypes=[("All files", "*")]
            )
        else:
            path = filedialog.askopenfilename(
                title="请选择模拟器根目录的 Mesen.exe",
                filetypes=[("Mesen", "Mesen.exe")]
            )

        if not path:
            return

        self.mesen_path = self.platform.validate_app_path(path)
        self.mesen_dir = os.path.dirname(path) if not path.endswith('.app') else path
        if IS_WINDOWS:
            self.platform.set_app_base_dir(self.mesen_dir)
        self._saved_mesen_path = self.mesen_path
        self._save_user_config()
        self.platform.ensure_dirs()
        self._patch_mesen_settings()
        self.lua_data_dir = self.platform.get_lua_data_dir(self.mesen_dir)
        self.bs_sfc_path = os.path.join(self.base_dir, "bszelda", "bs.sfc")
        self._update_base_dir_ui()
        self.btn_delete_save.config(state="normal")
        if not os.path.exists(self.bs_sfc_path):
            self._handle_missing_bios()
        else:
            # BIOS 存在，启用模式按钮
            self.btn_mode_m1.config(state="normal")
            self.btn_mode_m2.config(state="normal")
            # 修改主按钮为"第二步：请选择表/里模式"
            self.btn_select.config(text="第 2 步：选择 表模式 / 里模式 ", state="normal",
                                   command=self.activate_mode_selection)
            self._activate_chapter_selection()

    def delete_bios_save(self):
        save_file_path = os.path.join(self.platform.get_saves_dir(), "BsxBios.srm")
        confirm = messagebox.askyesno("删除存档确认",
                                      "确定要删除 BS-X BIOS 存档（BsxBios.srm）吗？\n此操作将清除游戏内注册的角色和所有广播游戏的进度。")
        if confirm:
            if os.path.exists(save_file_path):
                try:
                    os.remove(save_file_path)
                    messagebox.showinfo("成功", "存档文件已成功删除！")
                    logging.info(f"[存档管理] 已成功删除存档: {save_file_path}")
                except Exception as e:
                    messagebox.showerror("错误", f"无法删除存档文件: {e}")
            else:
                messagebox.showinfo("提示", "未找到存档文件，无需删除。")

    def _handle_missing_bios(self):
        messagebox.showinfo("BS-X BIOS 检查", "未检测到 BS-X BIOS，请手动选择 BIOS 文件。")
        bios_file = filedialog.askopenfilename(
            title="请选择 BS-X BIOS",
            filetypes=[("BS-X BIOS / SFC ROM", "*.sfc *.smc *.bin"), ("所有文件", "*.*")]
        )
        if bios_file:
            try:
                os.makedirs(os.path.dirname(self.bs_sfc_path), exist_ok=True)
                shutil.copy2(bios_file, self.bs_sfc_path)
                # BIOS 复制成功后，启用模式按钮
                self.btn_mode_m1.config(state="normal")
                self.btn_mode_m2.config(state="normal")
                # 修改主按钮为"第二步：请选择表/里模式"
                self.btn_select.config(text="第 2 步：选择 表模式 / 里模式 ", state="normal",
                                       command=self.activate_mode_selection)
                self._activate_chapter_selection()
            except (shutil.Error, OSError) as err:
                messagebox.showerror("错误", f"无法复制 BIOS: {err}")
        else:
            self.status_var.set("错误：BS-X BIOS 配置未完成")

    def _activate_chapter_selection(self):
        self.status_var.set("请选择需要推送的版本")
        self.btn_select.config(text="第 2 步：选择 表模式 / 里模式 ", state="normal",
                               command=self.activate_mode_selection)

    def activate_mode_selection(self):
        """第二步：提示用户选择表模式或里模式"""
        self.status_var.set("请点击下方 表模式 / 里模式 按钮进行选择")
        # 闪烁提示或者只是更新状态栏
        logging.info("[UI提示] 请选择表模式或里模式")

    def select_mode(self, mode):
        """选择表模式或里模式"""
        self.selected_mode = mode
        mode_name = "表模式" if mode == "m1" else "里模式"

        # 更新状态栏提示
        self.status_var.set(f"已选择：{mode_name}\n请选择推送 第几周 的广播")

        # 修改主按钮为"第三步：请选择第几周"
        self.btn_select.config(text="第 3 步：选择 第几周 ", command=self.activate_chapter_selection)

        # 启用章节按钮
        for btn in self.chapter_buttons:
            btn.config(state="normal")

        # 模式选择按钮变为不可选
        self.btn_mode_m1.config(state="disabled")
        self.btn_mode_m2.config(state="disabled")

        logging.info(f"[模式选择] 用户选择了 {mode_name}")

    def activate_chapter_selection(self):
        """第三步：提示用户选择第几周"""
        self.status_var.set("请点击下方 第 X 周 按钮选择")
        logging.info("[UI提示] 请选择第几周")

    def prepare_chapter(self, ch):
        if self.selected_mode is None:
            self.status_var.set("请选择需要推送的版本")
            return
        self.selected_chapter = ch
        mode_name = "表模式" if self.selected_mode == "m1" else "里模式"
        self.status_var.set(f"已锁定：{mode_name} 第 {ch} 周\n倒计时准备就绪")

        # 修改按钮为"第四步：点击后1分钟进行广播推送"
        self.btn_select.config(text="第 4 步：点击开始，将在1分钟后推送广播", command=self.start_countdown)

        # 章节按钮变为不可选
        for btn in self.chapter_buttons:
            btn.config(state="disabled")

        logging.info(f"[章节选择] 用户选择了第 {ch} 周")

    def _is_bios_ready(self):
        """检查 BIOS 是否已就绪"""
        return self.mesen_dir is not None and os.path.exists(self.bs_sfc_path)

    def reset_system(self):
        logging.info("[系统指令] 用户点击了重置系统按钮...")
        self.timer_running = False
        self._target_window = None
        self.settlement_audio_locked = False
        self.close_overlay()
        self._stop_audio()
        try:
            if pygame.mixer.get_init():
                pass
        except Exception as pg_reset_err:
            logging.warning(f"[音频重建] 重置系统时发现音频设备故障，正在尝试热重载音频层: {pg_reset_err}")
            try:
                pygame.mixer.quit()
                pygame.mixer.init()
            except pygame.error:
                pass

        if self.ganon_mute_timer:
            try:
                self.root.after_cancel(self.ganon_mute_timer)
            except (TclError, RuntimeError):
                pass
            self.ganon_mute_timer = None
        self.is_ganon_room_active = False
        self._set_mesen_mute(False)
        self.time_var.set("17:59:00")
        self.has_triggered_1800 = False
        self.is_ending_mode = False
        self.selected_chapter = None
        self.selected_mode = None
        self.btn_stop.config(state="disabled")

        # 重置模式选择 UI
        # 根据当前 Mesen 和 BIOS 状态决定恢复到哪一步
        if self.mesen_dir and self._is_bios_ready():
            # Mesen 已选且 BIOS 存在，恢复到第二步（选择模式）
            self.btn_mode_m1.config(state="normal")
            self.btn_mode_m2.config(state="normal")
            self.btn_select.config(state="normal", text="第 2 步：选择 表模式 / 里模式 ",
                                   command=self.activate_mode_selection)
            self.status_var.set("请选择需要推送的版本")
        elif self.mesen_dir:
            # Mesen 已选但 BIOS 不存在，恢复到第一步（需要选择 BIOS）
            self.btn_mode_m1.config(state="disabled")
            self.btn_mode_m2.config(state="disabled")
            self.btn_select.config(state="normal", text="第 1 步：选择模拟器根目录的 Mesen.exe ",
                                   command=self.select_mesen)
            self.status_var.set("请定位模拟器的主程序 Mesen.exe ")
        else:
            # 没有任何配置，恢复到初始状态
            self.btn_mode_m1.config(state="disabled")
            self.btn_mode_m2.config(state="disabled")
            self.btn_select.config(state="normal", text="第 1 步：选择模拟器根目录的 Mesen.exe ",
                                   command=self.select_mesen)
            self.status_var.set("请定位模拟器的主程序 Mesen.exe ")

        # 章节按钮全部禁用（等待模式选择后再启用）
        for btn in self.chapter_buttons:
            btn.config(state="disabled")

        self.chk_aspect.config(state="normal")
        self.chk_ntsc.config(state="normal")
        self.chk_fullscreen.config(state="normal")
        logging.info("[系统指令] 系统复位完毕。")

    @staticmethod
    def _retry_file_operation(operation, path, content=None, retries=3):
        """
        带重试机制的文件操作辅助函数
        operation: 'read' 或 'write'
        """
        for attempt in range(retries):
            try:
                if operation == 'read':
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read().strip()
                elif operation == 'write':
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(content)
                    return True
            except (FileNotFoundError, PermissionError, OSError) as e:
                if attempt == retries - 1:  # 最后一次失败才记录日志
                    logging.error(f"[文件操作] {operation} {path} 失败: {e}")
                time.sleep(RETRY_DELAY)
        return None if operation == 'read' else False

    def read_lua_file(self, filename, retries=2):
        if not self.lua_data_dir:
            return None
        path = os.path.join(self.lua_data_dir, filename)
        return self._retry_file_operation('read', path, retries=retries)

    def write_lua_file(self, filename, content, retries=3):
        """带有冲突重试机制的安全 Lua 文件写入"""
        if not self.lua_data_dir:
            return False
        path = os.path.join(self.lua_data_dir, filename)
        result = self._retry_file_operation('write', path, content=content, retries=retries)
        if not result:
            logging.error(f"[信号写入] 尝试重写 {filename} 失败，文件可能被系统或模拟器独占")
        return result

    def update_settlement_display(self):
        content = self.read_lua_file("result_data.txt")
        if content:
            try:
                def parse_kv(item):
                    parts = item.split(":", 1)
                    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("UNKNOWN", "0")

                data = dict(parse_kv(item) for item in content.split("|") if ":" in item)
                self.death_var.set(f"重新开始的次数: {data.get('DEATH', '0')} 次")
                self.heart_var.set(f"损失的心心数量: {data.get('HEART_LOSS', '0')} 个")
                self.rupee_var.set(f"所持的卢比数量: {data.get('RUPEE', '0')} 卢比")
                ganon_status = data.get('GANON', '0')
                self.ganon_var.set("已打倒加农" if ganon_status == '1' else "？？？？？")

                tf_val = int(data.get('TRIFORCE', '00'), 16)
                tf_icons = ["▲" if b == "1" else "△" for b in bin(tf_val)[2:].zfill(8)]
                self.triforce_var.set(f"三角力量收集情况:\n{' '.join(tf_icons)}")
                return tf_val
            except (ValueError, IndexError, KeyError) as err:
                logging.error(f"解析 result_data.txt 格式错误: {err}")
                return 0
        return 0

    def start_countdown(self):
        if not self.timer_running:
            if self.selected_mode is None:
                logging.error("[启动失败] 未选择游戏模式")
                self.status_var.set("请先选择模式（表模式/里模式）")
                self.reset_system()
                return
            if self.selected_chapter is None:
                logging.error("[启动失败] 未选择章节")
                self.status_var.set("请先选择第几周")
                self.reset_system()
                return

            # 禁用主按钮，防止重复点击
            self.btn_select.config(state="disabled")

            # 确保 Lua IPC 目录存在
            self.platform.ensure_dirs()

            # 确保模式按钮被禁用
            self.btn_mode_m1.config(state="disabled")
            self.btn_mode_m2.config(state="disabled")

            self.timer_running = True
            self.btn_stop.config(state="normal")
            for btn in self.chapter_buttons:
                btn.config(state="disabled")
            self.chk_aspect.config(state="disabled")
            self.chk_ntsc.config(state="disabled")
            self.chk_fullscreen.config(state="disabled")

            sat_dir = self.platform.get_satellaview_dir()
            reset_dir = os.path.join(self.base_dir, "bszelda", self.selected_mode, "0")
            logging.info(f"[广播数据] 源路径: {reset_dir}")
            logging.info(f"[广播数据] 目标路径: {sat_dir}")

            if os.path.exists(reset_dir):
                logging.info(f"[广播数据] 源文件夹存在，开始复制...")
                os.makedirs(sat_dir, exist_ok=True)
                files_copied = 0
                for item in os.listdir(reset_dir):
                    try:
                        shutil.copy2(os.path.join(reset_dir, item), os.path.join(sat_dir, item))
                        files_copied += 1
                    except (shutil.Error, OSError) as e:
                        logging.error(f"[复位失败] 无法拷贝初始文件 {item}: {e}")
                logging.info(f"[广播数据] 复制完成，共复制 {files_copied} 个文件")
            else:
                logging.error(f"[广播数据] 源文件夹不存在: {reset_dir}")

            bs_rom = self.bs_sfc_path
            bs_lua = get_resource_path("bs.lua")
            try:
                self.platform.launch_app(self.mesen_path, [bs_rom, bs_lua])
                self.root.after(2000, lambda: self._set_mesen_mute(False))
            except (subprocess.SubprocessError, OSError) as err:
                messagebox.showerror("启动失败", f"无法启动 Mesen: {err}")
                self.reset_system()
                return

            threading.Thread(target=self.clock_loop, daemon=True, name="ClockLoopThread").start()
            threading.Thread(target=self.signal_monitor_loop, daemon=True, name="SignalMonitorThread").start()

    def play_settlement_audio(self):
        logging.info("[音频匹配] 触发结算音频匹配...")
        self.settlement_audio_locked = True
        if self.ganon_mute_timer:
            try:
                self.root.after_cancel(self.ganon_mute_timer)
            except (TclError, RuntimeError):
                pass
        self._set_mesen_mute(True)

        rom_chapter = self.read_lua_file("chapter_signal.txt")
        final_ch = str(self.selected_chapter)
        if rom_chapter and rom_chapter != "FF":
            match = re.search(r'(\d+)', rom_chapter)
            if match:
                final_ch = match.group(1)

        audio_path = _resolve_file_case(os.path.join(self.base_dir, "bszelda", "wav", f"ED{final_ch}.wav"))
        if os.path.exists(audio_path):
            self._play_audio(audio_path, volume=1.0)
        else:
            logging.warning(f"[结局音频] 未找到文件 {os.path.basename(audio_path)}，将保持静音。")

    def signal_monitor_loop(self):
        logging.info("[线程监控] 信号监听轮询子线程启动成功。")
        while self.timer_running:
            if not self.settlement_active and self.read_lua_file("load_complete.txt") == "1":
                chapter_sig = self.read_lua_file("chapter_signal.txt")
                logging.info(f"[信号检测] 发现 Lua 载入完毕信号，目标章节/视频: {chapter_sig}")
                if chapter_sig and chapter_sig != "FF":
                    try:
                        self.write_lua_file("load_complete.txt", "0")
                    except OSError:
                        pass
                    # 强行切换到主线程中安全拉起剧情视频
                    self.root.after(0, lambda sig=chapter_sig: self.play_story_video(sig))

            if not self.settlement_active and self.read_lua_file("settle_trigger.txt") == "READY":
                logging.info("[信号检测] 发现结算触发器 READY 信号！准备结算渲染...")
                try:
                    self.write_lua_file("settle_trigger.txt", "DONE")
                except OSError:
                    pass
                self.play_settlement_audio()
                self.root.after(10000, self.show_custom_settlement_box)

            self._handle_ganon_audio_logic()
            time.sleep(SIGNAL_CHECK_INTERVAL)
        logging.info("[线程监控] 信号监听轮询子线程安全退出。")

    def _handle_ganon_audio_logic(self):
        if self.settlement_audio_locked:
            return
        spawn_state = self.read_lua_file("ganon_spawn.txt")
        result_content = self.read_lua_file("result_data.txt")
        if spawn_state is None:
            return
        is_defeated = False
        if result_content and "GANON:1" in result_content:
            is_defeated = True

        if spawn_state == "1":
            if is_defeated:
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                return
            if not self.is_ganon_room_active:
                self.is_ganon_room_active = True
                if self.ganon_mute_timer:
                    try:
                        self.root.after_cancel(self.ganon_mute_timer)
                    except (TclError, RuntimeError):
                        pass
                    self.ganon_mute_timer = None
                logging.info("[音频同步] 玩家进入加农房，音量渐弱...")
                self._fade_volume(0.0, duration=0.8)
        elif spawn_state == "0" and self.is_ganon_room_active:
            self.is_ganon_room_active = False
            logging.info("[音频同步] 离开加农房，3秒后恢复音量...")

            def delayed_restore():
                if not self.settlement_audio_locked and not self.is_ganon_room_active and self.timer_running:
                    logging.info("[音频同步] 触发渐强恢复...")
                    self._fade_volume(1.0, duration=1.2)

            if self.ganon_mute_timer:
                try:
                    self.root.after_cancel(self.ganon_mute_timer)
                except (TclError, RuntimeError):
                    pass
            self.ganon_mute_timer = self.root.after(3000, delayed_restore)

    def _get_mesen_window(self):
        if self._target_window and self._target_window.get("visible", True):
            return self._target_window
        self._target_window = self.platform.find_window(TARGET_WINDOW_TITLE)
        return self._target_window

    def play_story_video(self, video_name):
        """ 视频投影逻辑：在模拟器上方建立透明遮罩并调用 mpv 播放 """
        # 如果底层仍在进行异步毁灭，利用 after 50ms 后重新检查
        if getattr(self, '_mpv_destroying', False):
            logging.warning(f"[播放管道] 检测到旧 MPV 仍在后台销毁，延时 50ms 后重新尝试拉起视频: {video_name}")
            # 确保 50ms 后正确重试
            self.root.after(50, lambda: self.play_story_video(video_name))
            return

        logging.info(f"[播放管道] 收到剧情视频开播请求 -> 名称: {video_name}")
        self._set_mesen_mute(True)
        try:
            curr_time_str = self.time_var.get()
            curr_time_obj = datetime.strptime(curr_time_str, "%H:%M:%S")
            limit_time = datetime.strptime("18:05:52", "%H:%M:%S")
            story_len, wait_len = 135, 169
        except ValueError:
            return

        remaining_sec = (limit_time - curr_time_obj).total_seconds()
        if remaining_sec <= 0:
            logging.warning("[播放管道] 当前模拟时间已超出临界时间点，不予以播放视频。")
            self._set_mesen_mute(False)
            return

        force_wait_sync = (video_name == "wait" or remaining_sec <= story_len)
        if force_wait_sync:
            target_video = os.path.join(self.base_dir, "bszelda", "video", "wait.mp4")
            start_pos = max(0, int(wait_len - remaining_sec))
            self.is_waiting_video_mode = True
            logging.info(f"[播放管道] 切换为等待平铺视频(wait.mp4)，精准定位绝对进度秒数: {start_pos}")
        else:
            target_video = os.path.join(self.base_dir, "bszelda", "video", f"{video_name}.mp4")
            start_pos = 0
            self.is_waiting_video_mode = False

        if not os.path.exists(target_video):
            logging.error(f"[播放管道] 核心视频不存在: {target_video}，直接中断强制归还音频")
            self._set_mesen_mute(False)
            return

        if self.mpv_player:
            logging.info("[播放管道] 发现残余旧 MPV 实例，进行前置硬性终止...")
            try:
                # 先将事件回调置空，防止 terminate 产生多余的脏路由信号
                self.mpv_player.event_callback('end-file')(None)
                self.mpv_player.terminate()
            except(AttributeError, ValueError):
                pass
            self.mpv_player = None

        self.is_ending_mode = False

        if not self.overlay:
            try:
                logging.info("[UI框架] 创建全新的遮罩窗口 Toplevel")
                self.overlay = Toplevel(self.root)
                self.overlay.overrideredirect(True)
                self.overlay.attributes("-topmost", True)
            except TclError:
                self._set_mesen_mute(False)
                return

        if self.video_frame:
            try:
                logging.info("[UI框架] 清理旧视频渲染 Frame 容器")
                self.video_frame.destroy()
            except TclError:
                pass
            self.video_frame = None

        self.overlay.update_idletasks()

        self.video_frame = Frame(self.overlay, bg="black")
        self.video_frame.pack(fill="both", expand=True)

        if self.canvas:
            try:
                self.canvas.pack_forget()
            except TclError:
                pass

        self.overlay.update()

        try:
            logging.info(f"[MPV内核] 开始挂载底层 MPV C引擎，绑定渲染句柄ID: {self.video_frame.winfo_id()}")

            # 根据 UI 勾选框状态动态决定 MPV 参数
            # 获取当前比例模式
            is_4_3 = (self.keep_aspect_ratio_var.get() == "true")
            is_8_7 = (self.ntsc_ratio_var.get() == "true")
            is_keep = (is_4_3 or is_8_7)  # 只要勾选了任一比例，就启用 keepaspect

            if is_keep:
                mpv_keepaspect = 'yes'
                # 根据选择的比例设置具体宽高比
                if is_8_7:
                    mpv_aspect_override = '4/3'
                else:
                    mpv_aspect_override = '8/7'
            else:
                mpv_keepaspect = 'no'
                mpv_aspect_override = 'no'

            self.mpv_player = mpv.MPV(
                wid=str(self.video_frame.winfo_id()),
                keep_open='no',
                hwdec='auto',
                keepaspect=mpv_keepaspect,
                video_aspect_override=mpv_aspect_override
            )

            # 跨线程改用原生多线程 Timer 唤醒虚拟信号
            def _on_end_file_event(_event):
                if not getattr(self, 'timer_running', False) or getattr(self, 'settlement_active', False):
                    return
                logging.info("[MPV事件] 底层播放线程抛出 [视频放完] 事件！拉起绝对安全的异步脱离信号...")

                def _force_trigger():
                    logging.info(
                        "[异步事件机制] 定时触发完毕，正在向 Tkinter 主线程队列生成广播事件: <<VideoEndRouting>>")
                    try:
                        self.root.event_generate("<<VideoEndRouting>>", when="tail")
                    except Exception as ge:
                        logging.error(f"[异步事件机制] 事件注入崩溃: {ge}")

                # 完全脱离 MPV C内核线程的独立 Python 定时驱动器
                threading.Timer(0.1, _force_trigger).start()

            self.mpv_player.event_callback('end-file')(_on_end_file_event)
            logging.info("[MPV内核] C层事件回调回调端挂载成功。")

        except Exception as mpv_err:
            logging.error(f"初始化 MPV 核心失败: {mpv_err}")
            self._set_mesen_mute(False)
            return

        self._sync_overlay_geometry()

        if start_pos > 0:
            has_seeked = False

            @self.mpv_player.event_callback('file-loaded')
            def _on_file_loaded(_event):
                nonlocal has_seeked
                if not has_seeked:
                    try:
                        logging.info(f"[MPV内核] 接收到加载完成事件，强制执行跳转 seek -> {start_pos} 秒")
                        self.mpv_player.seek(start_pos, reference='absolute')
                        has_seeked = True
                    except Exception as se:
                        logging.error(f"[MPV内核] 跳转 seek 翻车: {se}")

        logging.info(f"[MPV内核] 开火放映视频: {os.path.basename(target_video)}")
        self.mpv_player.play(target_video)
        self._window_track_loop()

    def _safe_trigger_video_routing(self):
        """ 安全视频路由缓冲：在主线程中解绑回调，并利用异步子线程毁灭旧 MPV，确保不卡死主线程 """
        logging.info("[主线程时序] 确认收到 <<VideoEndRouting>> 事件，开始执行【普通视频防死锁安全连招】...")
        if not self.timer_running or self.settlement_active:
            logging.warning("[主线程时序] 检测到重置信号或结算已开，强行终止视频路由转换。")
            return

        if self.mpv_player:
            try:
                self.mpv_player.event_callback('end-file')(None)

                self._mpv_destroying = True  # 加锁告诉系统，底层正在毁灭实例
                old_player = self.mpv_player
                self.mpv_player = None

                def _async_routing_mpv_destruction(player_instance):
                    try:
                        player_instance['wid'] = 0
                        player_instance.stop()
                        player_instance.terminate()
                    except (AttributeError, ValueError):
                        pass
                    finally:
                        self._mpv_destroying = False  # 释放保护锁

                threading.Thread(target=_async_routing_mpv_destruction, args=(old_player,), daemon=True).start()

            except Exception as e:
                logging.error(f"[防死锁连招] 剥离动作出现未预期异常: {e}")

        # 主线程继续向前推进，执行下一步的视频/结算状态调度
        self._handle_video_end_routing()

    def _handle_video_end_routing(self):
        if not self.timer_running or self.settlement_active:
            return
        if not self.is_ending_mode and not self.is_waiting_video_mode:
            logging.info("[路由控制] 刚刚播放完普通剧情视频，开始无缝衔接至等待背景 wait.mp4...")
            self.play_story_video("wait")
            return
        logging.info("[路由控制] 等待视频或结局放映完成，开始执行全盘遮罩关闭...")
        self.close_overlay()

    def _window_track_loop(self):
        if not self.timer_running or not self.overlay or self.settlement_active:
            return

        if not self.is_ending_mode:
            if self.time_var.get() >= "18:05:52":
                logging.info("[时间追踪] 已到达临界时间点 18:05:52，强制卸载游戏内视频投影")
                self.close_overlay()
                return

        self._sync_overlay_geometry()
        self.root.after(30, self._window_track_loop)

    def _sync_overlay_geometry(self):
        m = self._get_mesen_window()
        cw, ch, cx, cy = 256, 224, 0, 0
        if m:
            try:
                self.dpi_scale = self.platform.get_dpi_scale(m)

                is_minimized = self.platform.is_minimized(m)

                # 状态平滑流转逻辑
                if is_minimized:
                    if not self._overlay_minimized_by_mesen:
                        self._overlay_minimized_by_mesen = True
                        if self.overlay and self.overlay.winfo_exists():
                            self.overlay.withdraw()
                            logging.info("[同步状态] 检测到模拟器已最小化，平滑隐藏遮罩窗口。")
                else:
                    if self._overlay_minimized_by_mesen:
                        self._overlay_minimized_by_mesen = False
                        if self.overlay and self.overlay.winfo_exists():
                            self.overlay.deiconify()
                            logging.info("[同步状态] 检测到模拟器已恢复正常，重新唤醒遮罩覆盖。")

                # 无论是否最小化，都必须强制完成真实的像素坐标和高宽计算
                is_fullscreen_optimize = (self.fullscreen_optimize_var.get() == "true")
                cw, ch, cx, cy = self._get_client_geometry(m, is_fullscreen_optimize)

                # 如果计算出的数据由于最小化产生异常（比如变成了0），强行用内置默认比例兜底
                if cw <= 0 or ch <= 0:
                    cw, ch, cx, cy = 256, 224, 0, 0

                if self.overlay and self.overlay.winfo_exists():
                    geom_str = f"{cw}x{ch}+{cx}+{cy}"
                    if self.overlay.geometry() != geom_str:
                        self.overlay.geometry(geom_str)

            except (TclError, Exception) as e:
                logging.debug(f"[同步几何体异常] {e}")

    def close_overlay(self):
        self._clear_animation_timers()
        logging.info("[释放进程] 全盘大清扫：正在彻底关闭、销毁、重置所有多媒体和图形容器...")
        self._overlay_minimized_by_mesen = False

        # 将 MPV 的物理超度完全移出主线程
        if self.mpv_player:
            def _async_mpv_destruction(player_instance):
                logging.info("[异步释放] 已在子线程中接管旧 MPV，开始离线剥离物理实例...")
                try:
                    # 子线程销毁前，同样先解绑事件
                    player_instance.event_callback('end-file')(None)
                    player_instance['wid'] = 0
                    player_instance.stop()
                    player_instance.terminate()
                    logging.info("[异步释放] 旧 MPV 实例在子线程中已彻底灰飞烟灭。")
                except Exception as ae:
                    logging.debug(f"[异步释放] 剥离期间细节捕获（可安全忽略）: {ae}")

            # 启动一个独立的、专门用来收容和毁灭 MPV 的僵尸线程
            threading.Thread(target=_async_mpv_destruction, args=(self.mpv_player,), daemon=True,
                             name="MpvDestructionThread").start()
            self.mpv_player = None

        # 主线程继续往下走，立刻恢复模拟器的音量
        self._set_mesen_mute(False)

        if self.is_ending_mode and self.mesen_dir and self.selected_mode:
            sat_dir = self.platform.get_satellaview_dir()
            reset_dir = os.path.join(self.base_dir, "bszelda", self.selected_mode, "0")
            if os.path.exists(reset_dir):
                os.makedirs(sat_dir, exist_ok=True)
                for item in os.listdir(reset_dir):
                    try:
                        shutil.copy2(os.path.join(reset_dir, item), os.path.join(sat_dir, item))
                    except (shutil.Error, OSError) as e:
                        logging.error(f"[结局复位失败] 无法拷贝初始文件 {item}: {e}")
                logging.info("[系统复位] 结局视频播放完毕，广播文件夹已恢复原始状态。")

        if self.overlay:
            try:
                logging.info("[释放进程] 主线程执行：彻底灰飞烟灭遮罩主窗口")
                self.overlay.destroy()
            except TclError:
                pass
            self.overlay, self.canvas, self.video_frame, self.settlement_active = None, None, None, False
        self.triforce_frames = []
        self.ui_refs = []
        self._last_geo = ""
        logging.info("[释放进程] 主线程大清扫指令下达完毕，主界面已完全恢复自由操作！")

    def clock_loop(self):
        logging.info("[线程监控] 模拟卫星时钟主循环子线程已开始工作。")
        try:
            start_real = time.time()
            start_sim = datetime.strptime(self.time_var.get(), "%H:%M:%S")
            while self.timer_running:
                curr_sim = (start_sim + timedelta(seconds=time.time() - start_real)).strftime("%H:%M:%S")
                self.time_var.set(curr_sim)
                if curr_sim == "18:00:00" and not self.has_triggered_1800:
                    logging.info("[卫星广播] 叮！时间已到 18:00:00！执行卫星信号强行空投推送...")
                    self.trigger_broadcast_and_audio()
                    self.has_triggered_1800 = True
                time.sleep(0.1)
        except ValueError:
            pass
        logging.info("[线程监控] 模拟卫星时钟主循环子线程已停止。")

    def trigger_broadcast_and_audio(self):
        sat_dir = self.platform.get_satellaview_dir()
        ch_dir = os.path.join(self.base_dir, "bszelda", self.selected_mode, str(self.selected_chapter))
        if os.path.exists(ch_dir):
            os.makedirs(sat_dir, exist_ok=True)
            for item in os.listdir(ch_dir):
                try:
                    shutil.copy2(os.path.join(ch_dir, item), os.path.join(sat_dir, item))
                except (shutil.Error, OSError):
                    continue

        # 音频文件路径不变（共用）
        audio_path = os.path.join(self.base_dir, "bszelda", "wav", f"{self.selected_chapter}.wav")
        self._play_audio(audio_path, volume=1.0)

    def _settlement_sync_loop(self):
        """ 针对结算界面的超高精度同步时钟：完美防范玩家在成绩单界面反复最小化/恢复 """
        # 防止重复注册
        if self._settlement_sync_active:
            return
        self._settlement_sync_active = True

        try:
            if not self.settlement_active or not self.overlay or not self.overlay.winfo_exists():
                return

            m = self._get_mesen_window()
            if m:
                try:
                    self.dpi_scale = self.platform.get_dpi_scale(m)
                    is_minimized = self.platform.is_minimized(m)

                    # 如果玩家在结算界面又最小化了模拟器
                    if is_minimized:
                        if not self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = True
                            self.overlay.withdraw()
                            logging.info("[结算守护] 玩家在结算单界面最小化了模拟器，已安全隐形。")

                        self.root.after(30, self._settlement_sync_loop)
                        return

                    # 如果玩家又把模拟器从任务栏里点开了（恢复正常）
                    else:
                        if self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = False
                            self.overlay.deiconify()
                            logging.info("[结算守护] 玩家恢复了模拟器，重新唤醒成绩单渲染。")

                            is_fullscreen_optimize = (self.fullscreen_optimize_var.get() == "true")
                            cw, ch, cx, cy = self._get_client_geometry(m, is_fullscreen_optimize)
                            if cw > 0 and ch > 0:
                                self.overlay.geometry(f"{cw}x{ch}+{cx}+{cy}")
                                self._render_settlement_content(cw, ch)

                    # 处于未最小化的普通游玩/拉伸状态
                    is_fullscreen_optimize = (self.fullscreen_optimize_var.get() == "true")
                    cw, ch, cx, cy = self._get_client_geometry(m, is_fullscreen_optimize)
                    if cw > 0 and ch > 0:
                        geo_str = f"{cw}x{ch}+{cx}+{cy}"
                        if self._last_geo != geo_str:
                            self._last_geo = geo_str
                            self.overlay.geometry(geo_str)
                            self._render_settlement_content(cw, ch)
                except (TclError, Exception) as loop_err:
                    logging.debug(f"[结算同步环异常] {loop_err}")
            else:
                # 如果玩家在结算单界面直接把模拟器关掉了，执行全盘安全熔断
                logging.warning("[结算守护] 丢失模拟器句柄，安全熔断清理。")
                self.close_overlay()
                return
        finally:
            self._settlement_sync_active = False

        # 维持高频同步
        if self.settlement_active and self.overlay and self.overlay.winfo_exists():
            self.root.after(100, self._settlement_sync_loop)

    def show_custom_settlement_box(self):
        if self.settlement_active:
            return
        logging.info("[结算渲染] 核心方法 show_custom_settlement_box 被调用，开始创建画布布局...")
        self.settlement_active = True

        if self.mpv_player:
            try:
                # 进入结算单前清除可能残留的回调
                self.mpv_player.event_callback('end-file')(None)
                self.mpv_player.terminate()
            except (AttributeError, ValueError):
                pass
            self.mpv_player = None

        cw, ch, cx, cy = 256, 224, 0, 0
        if not self.overlay:
            try:
                self.overlay = Toplevel(self.root)
                self.overlay.overrideredirect(True)
                self.overlay.attributes("-topmost", True)
            except TclError:
                self.settlement_active = False
                return

        if self.video_frame:
            try:
                self.video_frame.pack_forget()
            except TclError:
                pass

        if not self.canvas:
            self.canvas = Canvas(self.overlay, bg="black", highlightthickness=0)

        self.canvas.pack(fill="both", expand=True)

        m = self._get_mesen_window()
        if not m:
            self.close_overlay()
            return

        if m:
            self.dpi_scale = self.platform.get_dpi_scale(m)
            try:
                if self.platform.is_minimized(m):
                    self.platform.restore_window(m)
                    logging.info("[结算唤醒] 检测到模拟器处于最小化，已成功恢复并强抬至前台。")
                    time.sleep(0.05)
            except Exception as e:
                logging.debug(f"[结算唤醒异常] {e}")

            is_fullscreen_optimize = (self.fullscreen_optimize_var.get() == "true")
            cw, ch, cx, cy = self._get_client_geometry(m, is_fullscreen_optimize)

        self._last_geo = f"{cw}x{ch}+{cx}+{cy}"
        self.overlay.geometry(self._last_geo)

        # 顺便确保由于随动最小化被隐藏(withdraw)的遮罩被重新唤醒
        if self._overlay_minimized_by_mesen:
            self._overlay_minimized_by_mesen = False
            try:
                self.overlay.deiconify()
            except TclError:
                pass

        self._render_settlement_content(cw, ch)
        self._settlement_sync_loop()

    def _render_settlement_content(self, container_w, container_h):
        try:
            self._clear_animation_timers()
            self.canvas.delete("all")

            # 计算实际渲染区域（考虑保持比例）
            # 只要勾选了 4:3 或 8:7 任一，就启用比例保持
            is_keep_aspect = (self.keep_aspect_ratio_var.get() == "true" or self.ntsc_ratio_var.get() == "true")

            if is_keep_aspect:
                # SFC 原始比例 8:7 接近方形，但通常显示为 4:3（256x224）
                # 使用 256/224 = 1.142857 作为目标比例
                target_ratio = self._get_target_ratio()

                container_ratio = container_w / container_h

                if container_ratio > target_ratio:
                    # 容器更宽，上下黑边
                    render_h = container_h
                    render_w = int(render_h * target_ratio)
                    offset_x = (container_w - render_w) // 2
                    offset_y = 0
                else:
                    # 容器更高，左右黑边
                    render_w = container_w
                    render_h = int(render_w / target_ratio)
                    offset_x = 0
                    offset_y = (container_h - render_h) // 2
            else:
                # 拉伸填满
                render_w, render_h = container_w, container_h
                offset_x, offset_y = 0, 0

            # 可选：绘制黑边区域（保持画面比例的情况下填充黑色）
            if is_keep_aspect:
                self.canvas.create_rectangle(0, 0, container_w, container_h, fill="black", outline="")

            tf_val = self.update_settlement_display()
            rom_chapter = self.read_lua_file("chapter_signal.txt")
            final_ch = str(self.selected_chapter)
            if rom_chapter and rom_chapter != "FF":
                match = re.search(r'(\d+)', rom_chapter)
                if match:
                    final_ch = match.group(1)

            bg_path = get_resource_path("ui", "bg_result.png")
            if os.path.exists(bg_path):
                bg_img = Image.open(bg_path).resize((render_w, render_h), Image.Resampling.LANCZOS)
                self.bg_image_ref = ImageTk.PhotoImage(bg_img)
                self.canvas.create_image(offset_x, offset_y, anchor="nw", image=self.bg_image_ref)

            # 字号随渲染区域高度缩放
            f_size = -max(12, int(render_h * 0.055))
            tf_size_val = int(render_w * 0.055)

            # 布局坐标基于渲染区域（需要加上偏移量）
            center_x = offset_x + render_w / 2
            logical_w = render_w
            logical_h = render_h

            self.canvas.create_text(center_x, offset_y + logical_h * 0.12, text="BS 塞尔达传说成绩", fill="#FFFFFF",
                                    font=(_FONT_FAMILY, f_size))
            self.canvas.create_text(center_x, offset_y + logical_h * 0.20, text=f"— 第 {final_ch} 周 —", fill="#FFFFFF",
                                    font=(_FONT_FAMILY, f_size))

            label_x = offset_x + logical_w * 0.15
            value_x = offset_x + logical_w * 0.42
            curr_y = offset_y + logical_h * 0.30
            spacing = logical_h * 0.09

            self.canvas.create_text(label_x, curr_y, text=self.ganon_var.get(), fill="#FFFFFF",
                                    font=(_FONT_FAMILY, f_size), anchor="w")
            curr_y += spacing
            self.canvas.create_text(label_x, curr_y, text="三角力量", fill="#FFFFFF", font=(_FONT_FAMILY, f_size),
                                    anchor="w")

            # 三角力量图标
            self.triforce_frames = self._load_gif_frames(
                get_resource_path("ui", "triforce_on.gif"), (int(render_w * 0.055), int(render_w * 0.055)))
            off_path = get_resource_path("ui", "triforce_off.png")

            tf_bits = [int(b) for b in bin(tf_val)[2:].zfill(8)]
            for i, bit in enumerate(tf_bits):
                cur_x = (value_x + tf_size_val / 2) + (i * (tf_size_val + int(render_w * 0.01)))
                if bit == 1 and self.triforce_frames:
                    img_id = self.canvas.create_image(cur_x, curr_y, image=self.triforce_frames[0], anchor="center")
                    self._animate_triforce(img_id, 0)
                elif os.path.exists(off_path):
                    off_img = ImageTk.PhotoImage(
                        Image.open(off_path).resize((int(render_w * 0.055), int(render_w * 0.055)),
                                                    Image.Resampling.LANCZOS))
                    self.canvas.create_image(cur_x, curr_y, image=off_img, anchor="center")
                    self.ui_refs.append(off_img)

            labels = ["重新开始的次数", "损失的心心数量", "所持的卢比数量"]
            vals = [self.death_var.get().split(":")[-1].strip(), self.heart_var.get().split(":")[-1].strip(),
                    self.rupee_var.get().split(":")[-1].strip()]
            for i in range(3):
                curr_y += spacing
                self.canvas.create_text(label_x, curr_y, text=labels[i], fill="#FFFFFF", font=(_FONT_FAMILY, f_size),
                                        anchor="w")
                self.canvas.create_text(offset_x + logical_w * 0.85, curr_y, text=vals[i], fill="#FFFFFF",
                                        font=(_FONT_FAMILY, f_size), anchor="e")

            self.canvas.create_rectangle(offset_x + logical_w * 0.1, offset_y + logical_h * 0.85,
                                         offset_x + logical_w * 0.9, offset_y + logical_h * 0.93,
                                         outline="#F1C40F", width=2)

            self.canvas.create_text(center_x, offset_y + logical_h * 0.89, text="按下任意键继续", fill="#FFFFFF",
                                    font=(_FONT_FAMILY, f_size))

            logging.info("[结算渲染] 结算界面比例适配渲染完毕。")

        except TclError:
            pass
        except (AttributeError, FileNotFoundError, OSError) as data_err:
            logging.error(f"渲染数据加载失败: {data_err}")
        except Exception as unknown_err:
            logging.error(f"未预期的渲染异常: {unknown_err}")

        # 全局异步硬件盲听
        self._has_triggered_next = False
        self._stop_global_check = False

        def _trigger_next_page():
            if self._has_triggered_next:
                return
            self._has_triggered_next = True
            self._stop_global_check = True  # 刹车，停掉高频循环
            self.play_ending_video()

        # 强抓一次焦点，尽量建立友好的输入环境
        try:
            if self.overlay and self.overlay.winfo_exists():
                self.overlay.update()
                self.overlay.deiconify()
                self.overlay.focus_force()
                self.platform.bring_tk_to_front(self.overlay)
        except (TclError, OSError):
            pass

        # 启动 XInput + Win32 全局双重硬件轮询
        self._poll_global_input(_trigger_next_page)

    def _poll_global_input(self, trigger_callback):
        if hasattr(self, '_stop_global_check') and self._stop_global_check:
            return

        try:
            triggered = self.platform.poll_keyboard()
            if not triggered:
                triggered = self.platform.poll_gamepad()

            if triggered:
                trigger_callback()
                return

        except Exception as e:
            logging.debug(f"[内核级全局轮询异常] {e}")

        if self.overlay and self.overlay.winfo_exists():
            self.overlay.after(25, lambda: self._poll_global_input(trigger_callback))

    def _animate_triforce(self, img_id, frame_idx):
        if not self.overlay or not self.settlement_active:
            return
        try:
            idx = (frame_idx + 1) % len(self.triforce_frames)
            self.canvas.itemconfig(img_id, image=self.triforce_frames[idx])
            # 先创建定时器 ID
            t_id = self.root.after(100, lambda: self._animate_triforce(img_id, idx))
            # 先清理可能残留的相同 ID（防御性编程）
            self._anim_timers = [tid for tid in self._anim_timers if tid != t_id]
            # 立即加入列表，防止漏清理
            self._anim_timers.append(t_id)
        except (TclError, Exception):
            pass

    def _clear_animation_timers(self):
        """在重绘或销毁遮罩前，强行截断并清理所有残留的后台动画定时器"""
        for t_id in self._anim_timers:
            try:
                self.root.after_cancel(t_id)
            except TclError:
                pass
        self._anim_timers.clear()

    def play_ending_video(self):
        """ 播放结局视频逻辑（当成绩单被点击后触发） """
        logging.info("[结局跳转] 检测到成绩单被点击，准备退出成绩结算画布，切入大结局视频...")
        self.settlement_active = False
        rom_chapter = self.read_lua_file("chapter_signal.txt")
        final_ch = str(self.selected_chapter)
        if rom_chapter and rom_chapter != "FF":
            match = re.search(r'(\d+)', rom_chapter)
            if match:
                final_ch = match.group(1)
        video_p = _resolve_file_case(os.path.join(self.base_dir, "bszelda", "video", f"ED{final_ch}.mp4"))
        if os.path.exists(video_p):
            try:
                self.canvas.delete("all")
                self.canvas.unbind("<Button-1>")
                self.canvas.pack_forget()
            except (TclError, Exception):
                pass

            if self.video_frame:
                try:
                    self.video_frame.destroy()
                except TclError:
                    pass
                self.video_frame = None

            self.overlay.update_idletasks()

            self.video_frame = Frame(self.overlay, bg="black")
            self.video_frame.pack(fill="both", expand=True)

            if self.mpv_player:
                try:
                    # 切入大结局前清除可能残留的回调
                    self.mpv_player.event_callback('end-file')(None)
                    self.mpv_player.terminate()
                except (AttributeError, ValueError):
                    pass
                self.mpv_player = None

            self.is_ending_mode = True
            logging.info(f"[结局视频] 文件存在，开始组装 MPV。")

            self.overlay.update()

            try:
                # 根据 UI 勾选框状态动态决定大结局 MPV 参数
                # 获取当前比例模式
                is_4_3 = (self.keep_aspect_ratio_var.get() == "true")
                is_8_7 = (self.ntsc_ratio_var.get() == "true")
                is_keep = (is_4_3 or is_8_7)  # 只要勾选了任一比例，就启用 keepaspect

                if is_keep:
                    mpv_keepaspect = 'yes'
                    # 根据选择的比例设置具体宽高比
                    if is_8_7:
                        mpv_aspect_override = '4/3'
                    else:
                        mpv_aspect_override = '8/7'
                else:
                    mpv_keepaspect = 'no'
                    mpv_aspect_override = 'no'

                self.mpv_player = mpv.MPV(
                    wid=str(self.video_frame.winfo_id()),
                    keep_open='no',
                    hwdec='auto',
                    keepaspect=mpv_keepaspect,
                    video_aspect_override=mpv_aspect_override
                )

                def _on_ending_file_event(_event):
                    logging.info("[MPV事件] 底层结局视频播放完毕，抛出异步解离关闭信号...")

                    def _force_trigger_ending():
                        logging.info("[异步事件机制] 正在向 Tkinter 主线程队列生成结局关闭广播事件: <<EndingClose>>")
                        try:
                            self.root.event_generate("<<EndingClose>>", when="tail")
                        except Exception as ge:
                            logging.error(f"[异步事件机制] 结局事件注入崩溃: {ge}")

                    threading.Timer(0.1, _force_trigger_ending).start()

                self.mpv_player.event_callback('end-file')(_on_ending_file_event)
            except Exception as mpv_err:
                logging.error(f"结局视频初始化 MPV 核心失败: {mpv_err}")
                self.close_overlay()
                return

            self._sync_overlay_geometry()
            logging.info(f"[MPV内核] 开始放映大结局视频: {os.path.basename(video_p)}")
            self.mpv_player.play(video_p)
            self._window_track_loop()
        else:
            logging.warning(f"[结局视频] 未找到文件 {os.path.basename(video_p)}，直接关闭遮罩。")
            self.close_overlay()

    def _safe_trigger_ending_close(self):
        """ 结局视频放完后的安全关闭：解除结算音频锁，并利用异步全盘清扫机制规避死锁 """
        logging.info("[主线程时序] 确认收到 <<EndingClose>> 事件，开始执行【结局防死锁安全解锁连招】...")

        # 解除音量锁，确保后续能够顺利恢复模拟器的声音
        self.settlement_audio_locked = False

        # 如果 pygame 后台还在播放大结局的 WAV 广播配音，一并强行停掉
        self._stop_audio()
        try:
            if pygame.mixer.get_init():
                pass  # 音频系统正常
        except Exception as pg_unload_err:
            logging.warning(f"[音频重建] 监测到音频硬件异常，正在强行重启 Pygame 驱动: {pg_unload_err}")
            try:
                pygame.mixer.quit()
                pygame.mixer.init()
            except pygame.error:
                pass

        # 在主线程中只做事件解绑
        if self.mpv_player:
            try:
                self.mpv_player.event_callback('end-file')(None)
            except (AttributeError, ValueError):
                pass

        # 启动独立子线程去毁灭 MPV 实例，并在主线程中销毁组件、恢复模拟器声音、复位广播文件夹
        self.close_overlay()

    def run(self):
        logging.info("[系统启动] 广播模拟终端主窗体 Mainloop 开启。")
        try:
            self.root.mainloop()
        finally:
            if pygame.mixer.get_init():  # 检查是否已初始化
                pygame.mixer.quit()
        logging.info("[系统关闭] 主窗体 Mainloop 已退出。")


if __name__ == "__main__":
    BSXSimulator().run()
