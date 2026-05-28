# -*- coding: utf-8 -*-
import ctypes
try:
    # 优先尝试启用 Windows 10 推荐的 Per-Monitor (V2) DPI 感知
    # 如果对应的 shcore.dll 存在且函数可用，则执行该条
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    try:
        # 如果系统版本较低（如未升级的 Win8.1），回退到普通系统级 DPI 感知
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            # 如果处于非常古老的 Windows 7 / Vista 环境，调用最初代的全局 DPI 感知
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            # 极端的非 Windows 环境（如 Linux/Mac 测试编译）或彻底损坏的系统底层，执行无痛静默保底
            pass
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from ctypes import windll, wintypes
from datetime import datetime, timedelta
from tkinter import (Tk, Label, filedialog, StringVar, Checkbutton, Button, Frame, Toplevel, Canvas, messagebox,
                     TclError)

import pygame
import pygetwindow as gw
from PIL import Image, ImageTk

# 强行注入 MPV DLL 绝对路径
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_INTERNAL_DIR = os.path.join(_CURRENT_DIR, "_internal")

# 将 根目录 和 _internal 目录全部灌入 Windows 的 DLL 搜索网络
if hasattr(os, "add_dll_directory"):
    for dll_path in [_CURRENT_DIR, _INTERNAL_DIR]:
        if os.path.exists(dll_path):
            try:
                os.add_dll_directory(dll_path)
            except OSError:
                pass

# 同时确保环境变量 PATH 把这两个地方全部覆盖，防止老系统加载失败
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
    from pycaw.pycaw import AudioUtilities
except ImportError:
    logging.error("未检测到 pycaw 库，请执行: pip install pycaw")
    AudioUtilities = None

try:
    pygame.mixer.init()
except pygame.error as pg_err:
    logging.error(f"无法初始化音频设备: {pg_err}")

# 全局常量配置
TARGET_WINDOW_TITLE = 'Mesen - bs'  # 目标模拟器窗口的标题关键字
SIGNAL_CHECK_INTERVAL = 0.2  # 轮询 Lua 信号文件的时间间隔（秒）
RETRY_DELAY = 0.05  # 文件读取冲突时的重试延迟

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()  # 仅保留标准控制台输出
    ]
)


class BSXSimulator:
    """
    BS 塞尔达广播模拟终端主类
    负责：模拟时钟、视频遮罩投影、音频同步、结算数据渲染
    """

    def __init__(self):
        # 1. 初始化系统环境
        self.dpi_scale = self._get_system_dpi_scale()
        self.show_debug_ui = False

        self.root = Tk()
        self.root.title("BS 塞尔达传说 广播终端")

        window_w = int(340 * self.dpi_scale)
        window_h = int((920 if self.show_debug_ui else 420) * self.dpi_scale)
        self.root.geometry(f"{window_w}x{window_h}")
        self.root.resizable(False, False)

        # 2. 初始化路径变量
        self.mesen_path = ""
        self.mesen_dir = ""
        self.lua_data_dir = ""
        self.bs_sfc_path = ""

        # 3. 初始化状态控制变量
        self.selected_chapter = None
        self.timer_running = False
        self.has_triggered_1800 = False
        self._target_window = None
        self._anim_timers = []
        self._mesen_audio_session = None
        self._audio_lock = threading.Lock()
        self._mpv_destroying = False

        # 4. 视频遮罩与 UI 引用容器
        self.overlay = None
        self.canvas = None
        self.video_frame = None
        self.mpv_player = None
        self.bg_image_ref = None
        self.triforce_frames = []
        self.ui_refs = []

        # 5. 播放状态标志
        self.settlement_active = False
        self.is_ending_mode = False
        self.is_waiting_video_mode = False
        self.is_ganon_room_active = False
        self.ganon_mute_timer = None
        self.settlement_audio_locked = False
        self._last_geo = ""

        # 6. 绑定 UI 数据变量
        self.time_var = StringVar(value="17:59:00")
        self.status_var = StringVar(value="系统就绪：请选择主程序 Mesen.exe")
        self.death_var = StringVar(value="重新开始的次数: -- 次")
        self.heart_var = StringVar(value="损失的心心数量: -- 个")
        self.rupee_var = StringVar(value="所持的卢比数量: -- 卢比")
        self.triforce_var = StringVar(value="三角力量收集情况:\n△ △ △ △ △ △ △ △")
        self.ganon_var = StringVar(value="？？？？？")
        # 初始化画面比例控制变量，默认开启(true)
        self.keep_aspect_ratio_var = StringVar(value="true")
        self._overlay_minimized_by_mesen = False

        self._setup_ui()

        # 注册跨线程绝对安全的事件监听
        self.root.bind("<<VideoEndRouting>>", lambda e: self._safe_trigger_video_routing())
        self.root.bind("<<EndingClose>>", lambda e: self._safe_trigger_ending_close())

    @staticmethod
    def _get_system_dpi_scale(hwnd=None):
        """ 获取系统实时的 DPI 缩放比例 """
        try:
            # 如果传入了具体的窗口句柄，优先获取该窗口当前所在的具体屏幕的实时 DPI
            if hwnd and hasattr(ctypes.windll.user32, "GetDpiForWindow"):
                dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
                return dpi / 96.0

            # 现代 Windows 10 / 11 推荐的获取系统总 DPI 的标准 API
            if hasattr(ctypes.windll.user32, "GetDpiForSystem"):
                dpi = ctypes.windll.user32.GetDpiForSystem()
                return dpi / 96.0
        except Exception as e:
            logging.warning(f"[DPI获取] 现代API调用失败: {e}，将尝试传统方法保底")

        try:
            logpixelsx = 88
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, logpixelsx)
            ctypes.windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
        except Exception as e:
            logging.warning(f"[DPI获取] 传统方法也失败: {e}，默认返回 1.0")
            return 1.0

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
        if AudioUtilities is None:
            logging.warning("[音频同步] 由于未安装 pycaw 库，无法控制模拟器静音")
            return False

        with self._audio_lock:  # 强行加锁，保证同一时间只有一个线程能操作 pycaw
            # 1. 检查缓存，同时必须验证这个进程是不是还在正常运行
            if self._mesen_audio_session:
                try:
                    # 通过直接获取常驻进程状态探活
                    if self._mesen_audio_session.Process and self._mesen_audio_session.Process.status == "running":
                        self._mesen_audio_session.SetMute(1 if mute else 0, None)
                        logging.info(f"[音频同步] 通过安全缓存控制 Mesen 模拟器{'静音' if mute else '恢复音量'}")
                        return True
                    else:
                        raise ValueError("Mesen process is no longer running")
                except(ValueError, AttributeError, OSError):
                    logging.warning("[音频同步] 缓存的 Mesen 音频句柄已失效或进程已变动，尝试重新获取...")
                    self._mesen_audio_session = None

            # 2. 重新捕获
            try:
                sessions = AudioUtilities.GetAllSessions()
                for session in sessions:
                    if session.Process and session.Process.name().lower() == "mesen.exe":
                        self._mesen_audio_session = session.SimpleAudioVolume
                        self._mesen_audio_session.SetMute(1 if mute else 0, None)
                        logging.info(f"[音频同步] 成功捕获并重新缓存 Mesen 音频句柄")
                        return True
            except Exception as e:
                logging.error(f"音量控制或捕获失败: {e}")
            return False

    def _patch_mesen_settings(self):
        settings_path = os.path.join(self.mesen_dir, "settings.json")
        if not os.path.exists(settings_path):
            logging.warning(f"[配置] 未找到配置文件: {settings_path}")
            return

        try:
            with open(settings_path, 'r', encoding='utf-8-sig') as f:
                config = json.load(f)

            # 1. 开启 Lua 脚本系统 IO/OS 访问权限
            if "Debug" in config and "ScriptWindow" in config["Debug"]:
                config["Debug"]["ScriptWindow"]["AllowIoOsAccess"] = True
                logging.info("[配置] 成功开启脚本 IO/OS 访问权限")

            # 2. 修改 BSX 卫星时钟底座时间
            if "Snes" in config:
                config["Snes"]["BsxUseCustomTime"] = True
                config["Snes"]["BsxCustomTime"] = "09:59:00"
                logging.info("[配置] 成功修改 Bsx 时间")

            # 3. 清除手柄快进/快退快捷键绑定
            if "Preferences" in config and "ShortcutKeys" in config["Preferences"]:
                shortcut_keys_list = config["Preferences"]["ShortcutKeys"]
                if isinstance(shortcut_keys_list, list):
                    modified_shortcuts = 0
                    for shortcut_item in shortcut_keys_list:
                        # 匹配快进或快退项
                        if shortcut_item.get("Shortcut") in ["FastForward", "Rewind"]:
                            if "KeyCombination2" in shortcut_item:
                                shortcut_item["KeyCombination2"]["Key1"] = 0
                                modified_shortcuts += 1

                    if modified_shortcuts > 0:
                        logging.info(
                            f"[配置] 成功清除手柄快进/快退按键绑定 (共修改 {modified_shortcuts} 项)")

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

    def _get_client_geometry(self, hwnd):
        rect = wintypes.RECT()
        windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        point = wintypes.POINT(0, 0)
        windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
        offset = int(25 * self.dpi_scale)
        return w, h - offset, point.x, point.y + offset

    def _setup_ui(self):
        # 1. 字号转为绝对物理像素，与窗口框架 1:1 纯线性对齐
        dynamic_ui_font = ("Verdana", -int(26 * self.dpi_scale), "bold")
        dynamic_monitor_font = ("Verdana", -int(14 * self.dpi_scale), "bold")
        dynamic_tf_font = ("Verdana", -int(16 * self.dpi_scale), "bold")

        # 2. 界面间距随 DPI 实时调整物理高度
        pad_5 = int(5 * self.dpi_scale)
        pad_10 = int(10 * self.dpi_scale)
        pad_15 = int(15 * self.dpi_scale)
        pad_20 = int(20 * self.dpi_scale)

        time_frame = Frame(self.root, pady=pad_20)
        time_frame.pack()
        Label(time_frame, text="虚拟卫星时钟", font=dynamic_ui_font).pack(side="left")
        self.time_display = Label(time_frame, textvariable=self.time_var, font=dynamic_ui_font, fg="#e74c3c",
                                  padx=pad_10)
        self.time_display.pack(side="left")

        # wraplength 随 DPI 缩放，去掉高度死值，改用自动换行支撑，防止字变大后被拦腰截断
        self.status_label = Label(self.root, textvariable=self.status_var, fg="#2c3e50",
                                  wraplength=int(350 * self.dpi_scale), height=2, justify="center")
        self.status_label.pack(pady=pad_5)

        self.btn_select = Button(self.root, text="第一步：选择 Mesen.exe", command=self.select_mesen, width=30, height=2)
        self.btn_select.pack(pady=pad_10)

        self.ch_frame = Frame(self.root, pady=pad_5)
        self.ch_frame.pack()
        self.chapter_buttons = []
        for i in range(1, 5):
            btn = Button(self.ch_frame, text=f"第 {i} 周", state="disabled", width=6,
                         command=lambda ch=i: self.prepare_chapter(ch))
            btn.pack(side="left", padx=pad_5)
            self.chapter_buttons.append(btn)

        self.btn_stop = Button(self.root, text="重置状态", command=self.reset_system, width=30, height=2, state="disabled")
        self.btn_stop.pack(pady=pad_15)

        self.btn_delete_save = Button(self.root, text="删除存档（模拟器关闭状态下使用）", command=self.delete_bios_save,
                                      width=30, height=2,
                                      state="disabled", fg="#c0392b")
        self.btn_delete_save.pack(pady=pad_5)

        # 画面比例控制勾选框
        self.chk_aspect = Checkbutton(
            self.root,
            text="保持画面比例（防止拉伸变形）",
            variable=self.keep_aspect_ratio_var,
            onvalue="true",
            offvalue="false",
            activebackground=self.root.cget("bg")
        )
        self.chk_aspect.pack(pady=pad_5)

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

    def select_mesen(self):
        path = filedialog.askopenfilename(title="选择 Mesen.exe", filetypes=[("Mesen", "Mesen.exe")])
        if path:
            self.mesen_path = path
            self.mesen_dir = os.path.dirname(path)
            self._patch_mesen_settings()
            self.lua_data_dir = os.path.join(self.mesen_dir, "LuaScriptData", "bs")
            self.bs_sfc_path = os.path.join(self.mesen_dir, "bszelda", "bs.sfc")
            self.btn_delete_save.config(state="normal")
            if not os.path.exists(self.bs_sfc_path):
                self._handle_missing_bios()
            else:
                self._activate_chapter_selection()

    def delete_bios_save(self):
        if not self.mesen_dir:
            return
        save_file_path = os.path.join(self.mesen_dir, "saves", "BsxBios.srm")
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
        messagebox.showinfo("核心文件检查", "未检测到 BS-X BIOS，请手动选择。")
        bios_file = filedialog.askopenfilename(
            title="请选择 BS-X BIOS",
            filetypes=[("BS-X BIOS / SFC ROM", "*.sfc *.smc *.bin"), ("所有文件", "*.*")]
        )
        if bios_file:
            try:
                os.makedirs(os.path.dirname(self.bs_sfc_path), exist_ok=True)
                shutil.copy2(bios_file, self.bs_sfc_path)
                self._activate_chapter_selection()
            except (shutil.Error, OSError) as err:
                messagebox.showerror("错误", f"无法复制 BIOS: {err}")
        else:
            self.status_var.set("配置未完成。")

    def _activate_chapter_selection(self):
        self.status_var.set("系统就绪：请选择第几周的任务。")
        self.btn_select.config(text="第二步：请选择第几周...", state="disabled")
        for btn in self.chapter_buttons:
            btn.config(state="normal")

    def prepare_chapter(self, ch):
        self.selected_chapter = ch
        self.status_var.set(f"已锁定：第 {ch} 周\n倒计时准备就绪。")
        self.btn_select.config(text="第三步：点击后1分钟进行广播推送", command=self.start_countdown, state="normal")

    def reset_system(self):
        logging.info("[系统指令] 用户点击了重置系统按钮...")
        self.timer_running = False
        self._target_window = None
        self.settlement_audio_locked = False
        self.close_overlay()
        try:
            if pygame.mixer.get_init():
                pygame.mixer.music.set_volume(1.0)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
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
        self.btn_stop.config(state="disabled")
        self._activate_chapter_selection()
        self.chk_aspect.config(state="normal")
        logging.info("[系统指令] 系统复位完毕。")

    def read_lua_file(self, filename, retries=2):
        if not self.lua_data_dir:
            return None
        path = os.path.join(self.lua_data_dir, filename)
        for _ in range(retries):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except (FileNotFoundError, PermissionError, OSError):
                time.sleep(RETRY_DELAY)
        return None

    def write_lua_file(self, filename, content, retries=3):
        """带有冲突重试机制的安全 Lua 文件写入"""
        if not self.lua_data_dir:
            return False
        path = os.path.join(self.lua_data_dir, filename)
        for _ in range(retries):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return True
            except (PermissionError, OSError):
                time.sleep(RETRY_DELAY)
        logging.error(f"[信号写入] 尝试重写 {filename} 失败，文件可能被系统或模拟器独占")
        return False

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
            self.timer_running = True
            self.btn_select.config(state="disabled")
            self.btn_stop.config(state="normal")
            for btn in self.chapter_buttons:
                btn.config(state="disabled")
            self.chk_aspect.config(state="disabled")

            sat_dir = os.path.join(self.mesen_dir, "Satellaview")
            reset_dir = os.path.join(self.mesen_dir, "bszelda", "0")
            if os.path.exists(reset_dir):
                os.makedirs(sat_dir, exist_ok=True)
                for item in os.listdir(reset_dir):
                    try:
                        shutil.copy2(os.path.join(reset_dir, item), os.path.join(sat_dir, item))
                    except (shutil.Error, OSError) as e:
                        logging.error(f"[复位失败] 无法拷贝初始文件 {item}: {e}")
            bs_rom = self.bs_sfc_path
            bs_lua = os.path.join("bs.lua")
            try:
                subprocess.Popen([self.mesen_path, bs_rom, bs_lua])
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

        audio_path = os.path.join(self.mesen_dir, "bszelda", "wav", f"ED{final_ch}.wav")
        if os.path.exists(audio_path):
            try:
                pygame.mixer.music.set_volume(1.0)
                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()
                logging.info(f"[结局音频] 成功播放: {os.path.basename(audio_path)}")
            except Exception as e:
                logging.error(f"播放音频失败: {e}")
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
        if self._target_window and self._target_window.visible:
            return self._target_window
        wins = [w for w in gw.getWindowsWithTitle(TARGET_WINDOW_TITLE) if w.visible]
        self._target_window = wins[0] if wins else None
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
            target_video = os.path.join(self.mesen_dir, "bszelda", "video", "wait.mp4")
            start_pos = max(0, int(wait_len - remaining_sec))
            self.is_waiting_video_mode = True
            logging.info(f"[播放管道] 切换为等待平铺视频(wait.mp4)，精准定位绝对进度秒数: {start_pos}")
        else:
            target_video = os.path.join(self.mesen_dir, "bszelda", "video", f"{video_name}.mp4")
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
            is_keep = (self.keep_aspect_ratio_var.get() == "true")
            mpv_keepaspect = 'yes' if is_keep else 'no'
            mpv_aspect_override = '-1' if is_keep else 'no'

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
                hwnd = getattr(m, '_hWnd', None)
                if hwnd:
                    self.dpi_scale = self._get_system_dpi_scale(hwnd)

                    # 1. 声明 Windows 核心位置状态结构体
                    class WINDOWPLACEMENT(ctypes.Structure):
                        _fields_ = [
                            ("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                            ("ptMinPosition", ctypes.c_long * 2), ("ptMaxPosition", ctypes.c_long * 2),
                            ("rcNormalPosition", ctypes.c_long * 4)  # 包含正常状态下的 [left, top, right, bottom]
                        ]

                    wp = WINDOWPLACEMENT()
                    wp.length = ctypes.sizeof(WINDOWPLACEMENT)

                    is_minimized = False
                    if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                        # showCmd == 2 代表处于最小化状态
                        if wp.showCmd == 2:
                            is_minimized = True

                    # 2. 状态平滑流转逻辑
                    if is_minimized:
                        if not self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = True
                            if self.overlay and self.overlay.winfo_exists():
                                self.overlay.withdraw()  # 视觉隐藏，防穿帮
                                logging.info("[同步状态] 检测到模拟器已最小化，平滑隐藏遮罩窗口。")
                    else:
                        if self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = False
                            if self.overlay and self.overlay.winfo_exists():
                                self.overlay.deiconify()  # 恢复可见
                                logging.info("[同步状态] 检测到模拟器已恢复正常，重新唤醒遮罩覆盖。")

                    # 3. 无论是否最小化，都必须强制完成真实的像素坐标和高宽计算！
                    # 这样在视频初始化、切换视频瞬间，即使最小化，MPV 也能拿到安全有效的非零几何数据
                    cw, ch, cx, cy = self._get_client_geometry(hwnd)

                # 4. 如果计算出的数据由于最小化产生异常（比如变成了0），强行用内置默认比例兜底
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

        if self.is_ending_mode and self.mesen_dir:
            sat_dir = os.path.join(self.mesen_dir, "Satellaview")
            reset_dir = os.path.join(self.mesen_dir, "bszelda", "0")
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
        sat_dir = os.path.join(self.mesen_dir, "Satellaview")
        ch_dir = os.path.join(self.mesen_dir, "bszelda", str(self.selected_chapter))
        if os.path.exists(ch_dir):
            os.makedirs(sat_dir, exist_ok=True)
            for item in os.listdir(ch_dir):
                try:
                    shutil.copy2(os.path.join(ch_dir, item), os.path.join(sat_dir, item))
                except (shutil.Error, OSError):
                    continue

        audio_path = os.path.join(self.mesen_dir, "bszelda", "wav", f"{self.selected_chapter}.wav")
        if os.path.exists(audio_path):
            try:
                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()
                logging.info(f"[广播音频] 正在播放主流程广播配音: {os.path.basename(audio_path)}")
            except Exception as e:
                logging.error(f"广播音频播放失败: {e}")

    def _settlement_sync_loop(self):
        """ 针对结算界面的超高精度同步时钟：完美防范玩家在成绩单界面反复最小化/恢复 """
        if not self.settlement_active or not self.overlay or not self.overlay.winfo_exists():
            return

        m = self._get_mesen_window()
        if m:
            try:
                hwnd = getattr(m, '_hWnd', None)
                if hwnd:
                    self.dpi_scale = self._get_system_dpi_scale(hwnd)

                    # 1. 获取 Windows 窗口当前的真实显示放置状态
                    class WINDOWPLACEMENT(ctypes.Structure):
                        _fields_ = [
                            ("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                            ("ptMinPosition", ctypes.c_long * 2), ("ptMaxPosition", ctypes.c_long * 2),
                            ("rcNormalPosition", ctypes.c_long * 4)
                        ]

                    wp = WINDOWPLACEMENT()
                    wp.length = ctypes.sizeof(WINDOWPLACEMENT)

                    is_minimized = False
                    if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                        if wp.showCmd == 2:  # 2 = SW_SHOWMINIMIZED（最小化状态）
                            is_minimized = True

                    # 2. 如果玩家在结算界面又最小化了模拟器
                    if is_minimized:
                        if not self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = True
                            self.overlay.withdraw()  # 随动隐形，保证桌面不穿帮
                            logging.info("[结算守护] 玩家在结算单界面最小化了模拟器，已安全隐形。")

                        # 核心拦截！不要去执行后续错误的 0x0 图形刷新，静默等待复原
                        self.root.after(30, self._settlement_sync_loop)
                        return

                    # 3. 如果玩家又把模拟器从任务栏里点开了（恢复正常）
                    else:
                        if self._overlay_minimized_by_mesen:
                            self._overlay_minimized_by_mesen = False
                            self.overlay.deiconify()  # 随动恢复可见
                            logging.info("[结算守护] 玩家恢复了模拟器，重新唤醒成绩单渲染。")

                            # 刚复活瞬间强制洗牌，重新填满画面
                            cw, ch, cx, cy = self._get_client_geometry(hwnd)
                            if cw > 0 and ch > 0:
                                self.overlay.geometry(f"{cw}x{ch}+{cx}+{cy}")
                                self._render_settlement_content(cw, ch)

                    # 4. 处于未最小化的普通游玩/拉伸状态，执行你原汁原味的几何同步
                    cw, ch, cx, cy = self._get_client_geometry(hwnd)
                    if cw > 0 and ch > 0:
                        geo_str = f"{cw}x{ch}+{cx}+{cy}"
                        if self._last_geo != geo_str:
                            self._last_geo = geo_str
                            self.overlay.geometry(geo_str)
                            # 如果玩家拉伸或拖动了窗口，完美触发你的 Canvas 刷新逻辑
                            self._render_settlement_content(cw, ch)
            except (TclError, Exception) as loop_err:
                logging.debug(f"[结算同步环异常] {loop_err}")
        else:
            # 如果玩家在结算单界面直接把模拟器关掉了，执行全盘安全熔断
            logging.warning("[结算守护] 丢失模拟器句柄，安全熔断清理。")
            self.close_overlay()
            return

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

        hwnd = getattr(m, '_hWnd', None)
        if hwnd:
            self.dpi_scale = self._get_system_dpi_scale(hwnd)
            # 在这里拦截并强行恢复最小化的模拟器
            try:
                # 向 Windows 查询当前模拟器的放置状态
                class WINDOWPLACEMENT(ctypes.Structure):
                    _fields_ = [
                        ("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
                        ("ptMinPosition", ctypes.c_long * 2), ("ptMaxPosition", ctypes.c_long * 2),
                        ("rcNormalPosition", ctypes.c_long * 4)
                    ]

                wp = WINDOWPLACEMENT()
                wp.length = ctypes.sizeof(WINDOWPLACEMENT)

                if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
                    # showCmd == 2 代表当前确实处于最小化状态
                    if wp.showCmd == 2:
                        # 9 = SW_RESTORE (从任务栏恢复)
                        ctypes.windll.user32.ShowWindow(hwnd, 9)
                        # 5 = SW_SHOW (显式展示窗口)
                        ctypes.windll.user32.ShowWindow(hwnd, 5)
                        # 将模拟器强行拉到屏幕最前台
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                        logging.info("[结算唤醒] 检测到模拟器处于最小化，已成功恢复并强抬至前台。")

                        # 强抬可能需要微小的系统级刷新时间，让 Win32 响应后再读取最新位置
                        time.sleep(0.05)
            except Exception as e:
                logging.debug(f"[结算唤醒异常] {e}")

            # 此时模拟器已经物理复位，计算出来的绝对是最精准的正常高宽！
            cw, ch, cx, cy = self._get_client_geometry(hwnd)

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

    def _render_settlement_content(self, cw, ch):
        try:
            self._clear_animation_timers()
            self.canvas.delete("all")
            tf_val = self.update_settlement_display()
            rom_chapter = self.read_lua_file("chapter_signal.txt")
            final_ch = str(self.selected_chapter)
            if rom_chapter and rom_chapter != "FF":
                match = re.search(r'(\d+)', rom_chapter)
                if match:
                    final_ch = match.group(1)

            bg_path = os.path.join("ui", "bg_result.png")
            if os.path.exists(bg_path):
                bg_img = Image.open(bg_path).resize((cw, ch), Image.Resampling.LANCZOS)
                self.bg_image_ref = ImageTk.PhotoImage(bg_img)
                self.canvas.create_image(0, 0, anchor="nw", image=self.bg_image_ref)

            # 基础字号转换：直接随模拟器窗口高度等比缩放
            f_size = -max(12, int(ch * 0.055))
            tf_size_val = int(cw * 0.055)

            # 布局边界直接采用物理边界，保证缩放百分比随动
            logical_cw = cw
            logical_ch = ch

            self.canvas.create_text(logical_cw / 2, logical_ch * 0.12, text="BS 塞尔达传说成绩", fill="#FFFFFF",
                                    font=("Verdana", f_size))
            self.canvas.create_text(logical_cw / 2, logical_ch * 0.20, text=f"— 第 {final_ch} 周 —", fill="#FFFFFF",
                                    font=("Verdana", f_size))

            label_x, value_x, curr_y, spacing = logical_cw * 0.15, logical_cw * 0.42, logical_ch * 0.30, logical_ch * 0.09

            self.canvas.create_text(label_x, curr_y, text=self.ganon_var.get(), fill="#FFFFFF",
                                    font=("Verdana", f_size), anchor="w")
            curr_y += spacing
            self.canvas.create_text(label_x, curr_y, text="三角力量", fill="#FFFFFF", font=("Verdana", f_size),
                                    anchor="w")

            # 缩放适配的 GIF 帧大小（物理像素需求）
            self.triforce_frames = self._load_gif_frames(
                os.path.join("ui", "triforce_on.gif"), (int(cw * 0.055), int(cw * 0.055)))
            off_path = os.path.join("ui", "triforce_off.png")

            tf_bits = [int(b) for b in bin(tf_val)[2:].zfill(8)]
            for i, bit in enumerate(tf_bits):
                cur_x = (value_x + tf_size_val / 2) + (i * (tf_size_val + int(cw * 0.01)))
                if bit == 1 and self.triforce_frames:
                    img_id = self.canvas.create_image(cur_x, curr_y, image=self.triforce_frames[0], anchor="center")
                    self._animate_triforce(img_id, 0)
                elif os.path.exists(off_path):
                    # 物理像素缩放
                    off_img = ImageTk.PhotoImage(
                        Image.open(off_path).resize((int(cw * 0.055), int(cw * 0.055)), Image.Resampling.LANCZOS))
                    self.canvas.create_image(cur_x, curr_y, image=off_img, anchor="center")
                    self.ui_refs.append(off_img)

            labels = ["重新开始的次数", "损失的心心数量", "所持的卢比数量"]
            vals = [self.death_var.get().split(":")[-1].strip(), self.heart_var.get().split(":")[-1].strip(),
                    self.rupee_var.get().split(":")[-1].strip()]
            for i in range(3):
                curr_y += spacing
                self.canvas.create_text(label_x, curr_y, text=labels[i], fill="#FFFFFF", font=("Verdana", f_size),
                                        anchor="w")
                self.canvas.create_text(logical_cw * 0.85, curr_y, text=vals[i], fill="#FFFFFF",
                                        font=("Verdana", f_size),
                                        anchor="e")

            self.canvas.create_rectangle(logical_cw * 0.1, logical_ch * 0.85, logical_cw * 0.9, logical_ch * 0.93,
                                         outline="#F1C40F", width=2)

            self.canvas.create_text(logical_cw / 2, logical_ch * 0.89, text="按下任意键继续", fill="#FFFFFF",
                                    font=("Verdana", f_size))

            logging.info("[结算渲染] 画布第一页内容抗 DPI 缩放适配渲染完毕。")

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
                ctypes.windll.user32.SetForegroundWindow(self.overlay.winfo_id())
        except (TclError, OSError):
            pass

        # 启动 XInput + Win32 全局双重硬件轮询
        self._poll_global_input(_trigger_next_page)

    def _poll_global_input(self, trigger_callback):
        """ 全外设盲听：GetAsyncKeyState(键盘) + XInput底层(手柄) """
        if hasattr(self, '_stop_global_check') and self._stop_global_check:
            return

        try:
            triggered = False

            # 1. 跨进程全局键盘盲听（键盘）
            for vk_code in range(8, 256):
                if vk_code in (1, 2):  # 过滤鼠标左右键点击
                    continue
                if ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000:
                    logging.info(f"[全局硬件触发] 检测到键盘按键 VK_{vk_code} 按下")
                    triggered = True
                    break

            # 2. XInput 全局手柄检测（手柄）
            if not triggered:
                # 声明 XInput 手柄状态结构体类型
                class XinputButtons(ctypes.Structure):
                    _fields_ = [("wButtons", ctypes.c_ushort)]

                class XinputState(ctypes.Structure):
                    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", XinputButtons)]

                state = XinputState()

                # 盲听 0 到 3 号位（支持多达 4 个手柄接入）
                for user_index in range(4):
                    # 尝试调用 Windows 系统的 XInput1_4 或 9_1_0 驱动读取状态
                    # 0 代表 ERROR_SUCCESS（成功读取到该序号的手柄输入）
                    res = ctypes.windll.xinput1_4.XInputGetState(user_index, ctypes.byref(state))
                    if res != 0:
                        # 如果系统找不到 XInput1_4 (比如老系统)，自动退回旧版通用驱动尝试
                        res = ctypes.windll.xinput9_1_0.XInputGetState(user_index, ctypes.byref(state))

                    if res == 0:
                        # 读取当前时刻手柄按下的二进制按键掩码
                        buttons_mask = state.Gamepad.wButtons
                        # 只要掩码大于 0，说明玩家正在按手柄上的任意键（A/B/X/Y/方向键/肩键/菜单键等）
                        if buttons_mask > 0:
                            logging.info(f"[全局硬件触发] 检测到 {user_index} 号手柄按键掩码 {buttons_mask} 处于激活态")
                            triggered = True
                            break

            if triggered:
                trigger_callback()
                return

        except Exception as e:
            logging.debug(f"[内核级全局轮询异常] {e}")

        # 每 25 毫秒抽样一次（40 FPS，兼顾零延迟与超低 CPU 占用）
        if self.overlay and self.overlay.winfo_exists():
            self.overlay.after(25, lambda: self._poll_global_input(trigger_callback))

    def _animate_triforce(self, img_id, frame_idx):
        if not self.overlay or not self.settlement_active:
            return
        try:
            idx = (frame_idx + 1) % len(self.triforce_frames)
            self.canvas.itemconfig(img_id, image=self.triforce_frames[idx])
            # 记录生成的定时器 ID
            t_id = self.root.after(100, lambda: self._animate_triforce(img_id, idx))
            # 每次只保留当前生效的活跃动画句柄，防止长时间挂机列表无意义膨胀
            self._anim_timers = [tid for tid in self._anim_timers if tid != t_id]
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
        video_p = os.path.join(self.mesen_dir, "bszelda", "video", f"ED{final_ch}.mp4")
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
                is_keep = (self.keep_aspect_ratio_var.get() == "true")
                mpv_keepaspect = 'yes' if is_keep else 'no'
                mpv_aspect_override = '-1' if is_keep else 'no'

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

        # 1. 解除音量锁，确保后续能够顺利恢复模拟器的声音
        self.settlement_audio_locked = False

        # 2. 如果 pygame 后台还在播放大结局的 WAV 广播配音，一并强行停掉
        try:
            if pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except Exception as pg_unload_err:
            logging.warning(f"[音频重建] 监测到音频硬件异常，正在强行重启 Pygame 驱动: {pg_unload_err}")
            try:
                pygame.mixer.quit()
                pygame.mixer.init()
            except pygame.error:
                pass

        # 3. 在主线程中只做事件解绑
        if self.mpv_player:
            try:
                self.mpv_player.event_callback('end-file')(None)
            except (AttributeError, ValueError):
                pass

        # 4.启动独立子线程去毁灭 MPV 实例，并在主线程中销毁组件、恢复模拟器声音、复位广播文件夹
        self.close_overlay()

    def run(self):
        logging.info("[系统启动] BS 塞尔达传说 广播模拟终端主窗体 Mainloop 开启。")
        self.root.mainloop()
        logging.info("[系统关闭] 主窗体 Mainloop 已退出。")


if __name__ == "__main__":
    BSXSimulator().run()
