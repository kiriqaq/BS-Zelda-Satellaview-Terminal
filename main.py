# -*- coding: utf-8 -*-
import subprocess
import time
import os
import threading
import logging
import shutil
import ctypes
import re
from ctypes import windll, wintypes
from datetime import datetime, timedelta
from tkinter import (Tk, Label, filedialog, StringVar, Button, Frame, Toplevel, Canvas, messagebox, TclError)
import pygame
import cv2
import pygetwindow as gw
from PIL import Image, ImageTk

# --- 全局常量配置 ---
TARGET_WINDOW_TITLE = 'Mesen - bs'  # 目标模拟器窗口的标题关键字
SIGNAL_CHECK_INTERVAL = 0.2  # 轮询 Lua 信号文件的时间间隔（秒）
RETRY_DELAY = 0.05  # 文件读取冲突时的重试延迟
UI_FONT_BOLD = ("Verdana", 18, "bold")  # 标准粗体 UI 字体
MONITOR_FONT = ("Verdana", 14, "bold")  # 数据监视区字体
TRIFORCE_FONT = ("Verdana", 16, "bold")  # 三角力量专用字体

# --- 核心依赖库加载与初始化 ---
try:
    # pycaw 用于控制 Windows 系统的应用程序音量（实现模拟器自动静音）
    from pycaw.pycaw import AudioUtilities
except ImportError:
    logging.error("未检测到 pycaw 库，请执行: pip install pycaw")
    AudioUtilities = None

try:
    # 启用进程级 DPI 感知，防止在 Windows 高分屏缩放设置下界面模糊
    windll.shcore.SetProcessDpiAwareness(1)
except (AttributeError, OSError):
    pass

try:
    # 初始化 Pygame 音频混音器，用于播放广播音频（wav）
    pygame.mixer.init()
except pygame.error as pg_err:
    logging.error(f"无法初始化音频设备: {pg_err}")

# 配置全局日志格式：显示时间、级别和具体信息
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class BSXSimulator:
    """
    BS 塞尔达广播模拟终端主类
    负责：模拟时钟、视频遮罩投影、音频同步、结算数据渲染
    """

    def __init__(self):
        # 1. 初始化系统环境
        self.dpi_scale = self._get_system_dpi_scale()  # 获取当前系统缩放倍率
        self.show_debug_ui = False  # 是否显示底部的数据监视面板

        self.root = Tk()
        self.root.title("BS 塞尔达传说 广播终端")

        # 根据 DPI 缩放动态计算窗口尺寸
        window_w = int(400 * self.dpi_scale)
        window_h = int((820 if self.show_debug_ui else 420) * self.dpi_scale)
        self.root.geometry(f"{window_w}x{window_h}")
        self.root.resizable(False, False)

        # 2. 初始化路径变量（通过选择 Mesen.exe 动态确定）
        self.mesen_path = ""  # Mesen 主程序路径
        self.mesen_dir = ""  # Mesen 所在目录
        self.lua_data_dir = ""  # Lua 脚本交互文件的存放目录
        self.bs_sfc_path = ""  # BS-X BIOS 存放路径

        # 3. 初始化状态控制变量
        self.selected_chapter = None  # 当前选中的周目 (1-4)
        self.timer_running = False  # 虚拟时钟是否正在运行
        self.has_triggered_1800 = False  # 是否已触发 18:00 的广播逻辑
        self._target_window = None  # 缓存捕获到的模拟器窗口对象

        # 4. 视频遮罩与 UI 引用容器（防止图片被垃圾回收）
        self.overlay = None  # 悬浮在模拟器上方的 TopLevel 窗口
        self.canvas = None  # 遮罩上的画布
        self.cap = None  # OpenCV 视频流对象
        self.image_ptr = None  # 画布上的图片对象 ID
        self.tk_image_cache = None  # 视频帧缓存
        self.bg_image_ref = None  # 结算背景图缓存
        self.triforce_frames = []  # 三角力量 GIF 动画帧序列
        self.ui_refs = []  # 其他 UI 静态资源引用

        # 5. 播放状态标志
        self.settlement_active = False  # 是否正在显示结算界面
        self.is_ending_mode = False  # 是否处于最终结局视频播放状态
        self.is_waiting_video_mode = False  # 是否处于“等待广播开始”的循环视频状态
        self.is_ganon_room_active = False  # 是否处于加农房间（需静音广播）
        self.ganon_mute_timer = None  # 离开加农房后的延迟恢复定时器
        self.settlement_audio_locked = False  # 结算音频是否已锁定（防止被其他逻辑干扰音量）

        self._last_geo = ""  # 记录上次遮罩的位置，用于判断是否需要重绘

        # 6. 绑定 UI 数据变量
        self.time_var = StringVar(value="17:59:00")
        self.status_var = StringVar(value="系统就绪：请选择主程序 Mesen.exe")
        self.death_var = StringVar(value="重新开始的次数: -- 次")
        self.heart_var = StringVar(value="损失的心心数量: -- 个")
        self.rupee_var = StringVar(value="所持的卢比数量: -- 卢比")
        self.triforce_var = StringVar(value="三角力量收集情况:\n△ △ △ △ △ △ △ △")
        self.ganon_var = StringVar(value="？？？？？")

        self._setup_ui()

    @staticmethod
    def _get_system_dpi_scale():
        """ 通过 WinAPI 获取当前系统的屏幕缩放比例 (如 125% -> 1.25) """
        try:
            hdc = windll.user32.GetDC(0)
            dpi = windll.gdi32.GetDeviceCaps(hdc, 88)  # 88 代表 LOGPIXELSX
            windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
        except (AttributeError, OSError):
            return 1.0

    @staticmethod
    def _load_gif_frames(path, size):
        """ 加载 GIF 动画的所有帧并缩放到指定尺寸，用于三角力量旋转效果 """
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

    @staticmethod
    def _set_mesen_mute(mute=True):
        """ 利用 pycaw 精确控制 Mesen.exe 的系统混音器开关，实现“广播替代游戏 BGM” """
        if AudioUtilities is None:
            logging.warning("[音频同步] 由于未安装 pycaw 库，无法控制模拟器静音")
            return False
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if session.Process and session.Process.name().lower() == "mesen.exe":
                    volume = session.SimpleAudioVolume
                    volume.SetMute(1 if mute else 0, None)
                    logging.info(f"[音频同步] Mesen 模拟器已{'静音' if mute else '恢复音量'}")
                    return True
        except Exception as e:
            logging.error(f"音量控制失败: {e}")
        return False

    def _fade_volume(self, target_volume, duration=0.8):
        """ 广播音频的音量渐变（淡入/淡出），提升转场自然度 """

        def fade():
            try:
                start_volume = pygame.mixer.music.get_volume()
                steps = 20
                interval = duration / steps
                delta = (target_volume - start_volume) / steps
                for i in range(steps):
                    if self.settlement_audio_locked:  # 结算期间不进行渐变干扰
                        return
                    new_vol = start_volume + delta * (i + 1)
                    pygame.mixer.music.set_volume(max(0.0, min(1.0, new_vol)))
                    time.sleep(interval)
                if not self.settlement_audio_locked:
                    pygame.mixer.music.set_volume(target_volume)
            except Exception as e:
                logging.error(f"音量渐变执行失败: {e}")

        threading.Thread(target=fade, daemon=True).start()

    def _get_client_geometry(self, hwnd):
        """ 获取模拟器渲染区域（Client Area）的绝对坐标和尺寸，避开标题栏和边框 """
        rect = wintypes.RECT()
        windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        point = wintypes.POINT(0, 0)
        windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
        # 针对 Mesen 渲染布局的微调偏移量
        offset = int(25 * self.dpi_scale)
        return w, h - offset, point.x, point.y + offset

    def _setup_ui(self):
        """ 构建 Tkinter 主界面布局 """
        dynamic_ui_font = ("Verdana", int(18 * self.dpi_scale), "bold")
        dynamic_monitor_font = ("Verdana", int(14 * self.dpi_scale), "bold")
        dynamic_tf_font = ("Verdana", int(16 * self.dpi_scale), "bold")

        # 1. 虚拟时钟显示区
        time_frame = Frame(self.root, pady=20)
        time_frame.pack()
        Label(time_frame, text="虚拟卫星时钟", font=dynamic_ui_font).pack(side="left")
        self.time_display = Label(time_frame, textvariable=self.time_var, font=dynamic_ui_font, fg="#e74c3c", padx=10)
        self.time_display.pack(side="left")

        # 2. 状态提示与引导按钮
        self.status_label = Label(self.root, textvariable=self.status_var, fg="#2c3e50",
                                  wraplength=int(350 * self.dpi_scale), height=3, justify="center")
        self.status_label.pack(pady=5)
        self.btn_select = Button(self.root, text="第一步：选择 Mesen.exe", command=self.select_mesen, width=30, height=2)
        self.btn_select.pack(pady=10)

        # 3. 周目选择区
        self.ch_frame = Frame(self.root, pady=5)
        self.ch_frame.pack()
        self.chapter_buttons = []
        for i in range(1, 5):
            btn = Button(self.ch_frame, text=f"第 {i} 周", state="disabled", width=6,
                         command=lambda ch=i: self.prepare_chapter(ch))
            btn.pack(side="left", padx=5)
            self.chapter_buttons.append(btn)

        self.btn_stop = Button(self.root, text="重置状态", command=self.reset_system, width=30, state="disabled")
        self.btn_stop.pack(pady=15)

        # 4. 调试/监视面板（仅在 self.show_debug_ui 为 True 时可见）
        if self.show_debug_ui:
            monitor_section = Frame(self.root, pady=10, padx=20, relief="groove", borderwidth=2)
            monitor_section.pack(fill="x", padx=20, pady=10)
            Label(monitor_section, text="[ 结算数据监视中心 ]", font=dynamic_ui_font, fg="#2980b9").pack(pady=(0, 5))
            result_subframe = Frame(monitor_section)
            result_subframe.pack(fill="x")
            Label(result_subframe, textvariable=self.ganon_var, font=dynamic_monitor_font, fg="#e74c3c").pack()
            Label(result_subframe, textvariable=self.triforce_var, font=dynamic_tf_font, fg="#f39c12",
                  wraplength=int(300 * self.dpi_scale)).pack()
            Label(result_subframe, textvariable=self.death_var, font=dynamic_monitor_font).pack(anchor="w")
            Label(result_subframe, textvariable=self.heart_var, font=dynamic_monitor_font).pack(anchor="w")
            Label(result_subframe, textvariable=self.rupee_var, font=dynamic_monitor_font).pack(anchor="w")
            self.btn_test = Button(monitor_section, text="立即测试数据", command=self.update_settlement_display,
                                   bg="#ecf0f1")
            self.btn_test.pack(pady=5, fill="x")

    def select_mesen(self):
        """ 处理用户选择模拟器的行为，并初始化相关路径 """
        path = filedialog.askopenfilename(title="选择 Mesen.exe", filetypes=[("Mesen", "Mesen.exe")])
        if path:
            self.mesen_path = path
            self.mesen_dir = os.path.dirname(path)
            # 约定：Lua 数据交换文件必须放在 Mesen/LuaScriptData/bs 目录下
            self.lua_data_dir = os.path.join(self.mesen_dir, "LuaScriptData", "bs")
            self.bs_sfc_path = os.path.join(self.mesen_dir, "bszelda", "bs.sfc")
            if not os.path.exists(self.bs_sfc_path):
                self._handle_missing_bios()
            else:
                self._activate_chapter_selection()

    def _handle_missing_bios(self):
        """ 引导用户配置 BS-X BIOS 核心文件 """
        messagebox.showinfo("核心文件检查", "未检测到 BS-X BIOS，请手动选择。")
        bios_file = filedialog.askopenfilename(title="请选择 BS-X BIOS", filetypes=[("SFC ROM", "*.sfc *.smc")])
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
        """ 开启周任务选择阶段 """
        self.status_var.set("系统就绪：请选择第几周的任务。")
        self.btn_select.config(text="第二步：请选择第几周...", state="disabled")
        for btn in self.chapter_buttons:
            btn.config(state="normal")

    def prepare_chapter(self, ch):
        """ 锁定选中的周 """
        self.selected_chapter = ch
        self.status_var.set(f"已锁定：第 {ch} 周\n倒计时准备就绪。")
        self.btn_select.config(text="第三步：点击后1分钟进行广播推送", command=self.start_countdown, state="normal")

    def reset_system(self):
        """ 全局重置：停止时钟、关闭视频、停止音频、恢复模拟器声音 """
        self.timer_running = False
        self._target_window = None
        self.settlement_audio_locked = False
        if self.cap:
            self.cap.release()
            self.cap = None
        self.close_overlay()
        if pygame.mixer.get_init():
            pygame.mixer.music.set_volume(1.0)
            pygame.mixer.music.stop()
            pygame.mixer.music.unload()
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

    def read_lua_file(self, filename, retries=2):
        """ 安全读取 Lua 脚本生成的数据文件，带简单的冲突重试机制 """
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

    def update_settlement_display(self):
        """ 解析 result_data.txt，将游戏内存提取的成绩转换为 UI 可读数据 """
        content = self.read_lua_file("result_data.txt")
        if content:
            try:
                def parse_kv(item):
                    parts = item.split(":", 1)
                    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("UNKNOWN", "0")

                # 数据格式示例：DEATH:0|HEART_LOSS:5|TRIFORCE:FF
                data = dict(parse_kv(item) for item in content.split("|") if ":" in item)
                self.death_var.set(f"重新开始的次数: {data.get('DEATH', '0')} 次")
                self.heart_var.set(f"损失的心心数量: {data.get('HEART_LOSS', '0')} 个")
                self.rupee_var.set(f"所持的卢比数量: {data.get('RUPEE', '0')} 卢比")
                ganon_status = data.get('GANON', '0')
                self.ganon_var.set("已打倒加农" if ganon_status == '1' else "？？？？？")
                # 三角力量使用位图运算解析（1字节代表8块碎片）
                tf_val = int(data.get('TRIFORCE', '00'), 16)
                tf_icons = ["▲" if b == "1" else "△" for b in bin(tf_val)[2:].zfill(8)]
                self.triforce_var.set(f"三角力量收集情况:\n{' '.join(tf_icons)}")
                return tf_val
            except (ValueError, IndexError, KeyError) as err:
                logging.error(f"解析 result_data.txt 格式错误: {err}")
                return 0
        return 0

    def start_countdown(self):
        """ 正式启动流程：开启模拟器，并运行时间逻辑线程与文件监视线程 """
        if not self.timer_running:
            self.timer_running = True
            self.btn_select.config(state="disabled")
            self.btn_stop.config(state="normal")
            for btn in self.chapter_buttons:
                btn.config(state="disabled")

            bs_rom = self.bs_sfc_path
            bs_lua = os.path.join("bs.lua")
            try:
                # 启动 Mesen 模拟器，并加载对应的 ROM 和 LUA 脚本
                subprocess.Popen([self.mesen_path, bs_rom, bs_lua])
                self.root.after(2000, lambda: self._set_mesen_mute(False))
            except (subprocess.SubprocessError, OSError) as err:
                messagebox.showerror("启动失败", f"无法启动 Mesen: {err}")
                self.reset_system()
                return

            # 开启双后台线程：一个跑表，一个盯信号
            threading.Thread(target=self.clock_loop, daemon=True).start()
            threading.Thread(target=self.signal_monitor_loop, daemon=True).start()

    def play_settlement_audio(self):
        """ 结局触发：静音游戏 BGM，播放对应的结局旁白（wav） """
        logging.info("[系统] 触发结算音频匹配...")
        self.settlement_audio_locked = True
        if self.ganon_mute_timer:
            try:
                self.root.after_cancel(self.ganon_mute_timer)
            except (TclError, RuntimeError):
                pass
        self._set_mesen_mute(True)

        # 确定播放哪个周目的结局音频（优先以 Lua 实时信号为准）
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
        """ 后台死循环：实时监控 Lua 发出的各种交互信号（加载视频、结算、加农房检测） """
        while self.timer_running:
            # 信号1：检测到游戏加载完成信号，触发剧情介绍视频
            if not self.settlement_active and self.read_lua_file("load_complete.txt") == "1":
                chapter_sig = self.read_lua_file("chapter_signal.txt")
                if chapter_sig and chapter_sig != "FF":
                    try:
                        # 消费完信号后立即回写 0，防止重复触发
                        with open(os.path.join(self.lua_data_dir, "load_complete.txt"), "w") as f:
                            f.write("0")
                    except OSError:
                        pass
                    self.root.after(0, lambda sig=chapter_sig: self.play_story_video(sig))

            # 信号2：检测到结算触发信号
            if not self.settlement_active and self.read_lua_file("settle_trigger.txt") == "READY":
                try:
                    with open(os.path.join(self.lua_data_dir, "settle_trigger.txt"), "w") as f:
                        f.write("DONE")
                except OSError:
                    pass
                self.play_settlement_audio()
                # 延迟10秒弹出成绩单，等待旁白铺垫
                self.root.after(10000, self.show_custom_settlement_box)

            # 信号3：加农房特殊逻辑控制
            self._handle_ganon_audio_logic()
            time.sleep(SIGNAL_CHECK_INTERVAL)

    def _handle_ganon_audio_logic(self):
        """ 特殊逻辑：当玩家进入最终 Boss 加农房时，广播音频静音，模拟当年的效果 """
        if self.settlement_audio_locked:
            return
        spawn_state = self.read_lua_file("ganon_spawn.txt")
        result_content = self.read_lua_file("result_data.txt")
        if spawn_state is None:
            return
        is_defeated = False
        if result_content and "GANON:1" in result_content:
            is_defeated = True

        if spawn_state == "1":  # 进房
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
        elif spawn_state == "0" and self.is_ganon_room_active:  # 出房
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
        """ 查找并定位模拟器窗口实例 """
        if self._target_window and self._target_window.visible:
            return self._target_window
        wins = [w for w in gw.getWindowsWithTitle(TARGET_WINDOW_TITLE) if w.visible]
        self._target_window = wins[0] if wins else None
        return self._target_window

    def play_story_video(self, video_name):
        """ 视频投影逻辑：在模拟器上方建立透明遮罩并播放 mp4 """
        self._set_mesen_mute(True)
        try:
            curr_time_str = self.time_var.get()
            curr_time_obj = datetime.strptime(curr_time_str, "%H:%M:%S")
            limit_time = datetime.strptime("18:05:52", "%H:%M:%S")  # 广播正式开始的时间点
            story_len, wait_len = 135, 168
        except ValueError:
            return

        remaining_sec = (limit_time - curr_time_obj).total_seconds()
        if remaining_sec <= 0:
            self._set_mesen_mute(False)
            return

        # 根据剩余时间决定是播放剧情简介还是循环等待视频
        force_wait_sync = (video_name == "wait" or remaining_sec <= story_len)
        if force_wait_sync:
            target_video = os.path.join(self.mesen_dir, "bszelda", "video", "wait.mp4")
            start_pos = max(0, int(wait_len - remaining_sec))  # 时间戳对齐
            self.is_waiting_video_mode = True
        else:
            target_video = os.path.join(self.mesen_dir, "bszelda", "video", f"{video_name}.mp4")
            start_pos = 0
            self.is_waiting_video_mode = False

        if not os.path.exists(target_video):
            self._set_mesen_mute(False)
            return

        if self.cap:
            self.cap.release()
        self.cap = cv2.VideoCapture(target_video)
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(start_pos * fps))
        self.is_ending_mode = False

        # 创建或更新全顶层悬浮窗口 (Toplevel)
        if not self.overlay:
            try:
                self.overlay = Toplevel(self.root)
                self.overlay.overrideredirect(True)  # 去掉窗口边框和标题栏
                self.overlay.attributes("-topmost", True)  # 永远置顶
                self.canvas = Canvas(self.overlay, bg="black", highlightthickness=0)
                self.canvas.pack(fill="both", expand=True)
            except TclError:
                self._set_mesen_mute(False)
                return
        self._render_loop()

    def _render_loop(self):
        """ 视频渲染主循环：利用 OpenCV 读取帧并更新到 Tkinter 画布上 """
        if not self.timer_running or not self.overlay or self.settlement_active:
            return
        start_proc = time.time()
        ret, frame = self.cap.read()
        if not ret:
            # 视频放完后，如果是剧情介绍，则跳转到等待视频；否则关闭
            if not self.is_ending_mode and not self.is_waiting_video_mode:
                self.play_story_video("wait")
                return
            self.close_overlay()
            return

        if not self.is_ending_mode:
            # 如果到达 18:05:52 广播正式点，强制关闭视频遮罩回到游戏界面
            if self.time_var.get() >= "18:05:52":
                self.close_overlay()
                return

        m = self._get_mesen_window()
        cw, ch, cx, cy = 256, 224, 0, 0
        if m:
            try:
                hwnd = getattr(m, '_hWnd', None)
                if hwnd:
                    # 动态追踪模拟器位置和大小
                    cw, ch, cx, cy = self._get_client_geometry(hwnd)
                if self.overlay.geometry() != f"{cw}x{ch}+{cx}+{cy}":
                    self.overlay.geometry(f"{cw}x{ch}+{cx}+{cy}")

                # 图像转换：OpenCV (BGR) -> PIL (RGB) -> Tkinter PhotoImage
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = ImageTk.PhotoImage(Image.fromarray(cv2.resize(rgb, (cw, ch))))

                if self.image_ptr is None:
                    self.image_ptr = self.canvas.create_image(0, 0, anchor="nw", image=img)
                else:
                    self.canvas.itemconfig(self.image_ptr, image=img)
                self.tk_image_cache = img  # 关键：缓存引用防止回收导致白屏
            except (TclError, Exception):
                pass

        # 控制渲染帧率，使其匹配视频 FPS
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 30
        delay = max(1, int((1000 / fps) - (time.time() - start_proc) * 1000))
        self.root.after(delay, self._render_loop)

    def close_overlay(self):
        """ 安全销毁遮罩窗口和相关资源 """
        if self.cap:
            self.cap.release()
            self.cap = None
        self._set_mesen_mute(False)
        if self.overlay:
            try:
                self.overlay.destroy()
            except TclError:
                pass
            self.overlay, self.image_ptr, self.settlement_active = None, None, False
        self.tk_image_cache = None
        self.triforce_frames = []
        self.ui_refs = []
        self._last_geo = ""

    def clock_loop(self):
        """ 模拟“广播时间”的虚拟时钟循环 """
        try:
            start_real = time.time()
            start_sim = datetime.strptime(self.time_var.get(), "%H:%M:%S")
            while self.timer_running:
                # 模拟时钟 = 初始虚拟时间 + (当前系统时间 - 启动时系统时间)
                curr_sim = (start_sim + timedelta(seconds=time.time() - start_real)).strftime("%H:%M:%S")
                self.time_var.set(curr_sim)
                if curr_sim == "18:00:00" and not self.has_triggered_1800:
                    self.trigger_broadcast_and_audio()
                    self.has_triggered_1800 = True
                time.sleep(0.1)
        except ValueError:
            pass

    def trigger_broadcast_and_audio(self):
        """ 18:00 关键动作：拷贝卫星广播文件到模拟器目录，并开始播放音频 wav """
        sat_dir = os.path.join(self.mesen_dir, "Satellaview")
        ch_dir = os.path.join(self.mesen_dir, "bszelda", str(self.selected_chapter))
        if os.path.exists(ch_dir):
            os.makedirs(sat_dir, exist_ok=True)
            for item in os.listdir(ch_dir):
                try:
                    # 模拟卫星下发数据：将文件拷入模拟器预设目录
                    shutil.copy2(os.path.join(ch_dir, item), os.path.join(sat_dir, item))
                except (shutil.Error, OSError):
                    continue

        audio_path = os.path.join(self.mesen_dir, "bszelda", "wav", f"{self.selected_chapter}.wav")
        if os.path.exists(audio_path):
            try:
                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()
            except Exception as e:
                logging.error(f"广播音频播放失败: {e}")

    def _settlement_sync_loop(self):
        """ 结算界面的位置同步循环（确保遮罩始终跟着模拟器窗口走） """
        if not self.settlement_active or not self.overlay:
            return
        m = self._get_mesen_window()
        cw, ch, cx, cy = 256, 224, 0, 0
        if m:
            try:
                hwnd = getattr(m, '_hWnd', None)
                if hwnd:
                    cw, ch, cx, cy = self._get_client_geometry(hwnd)
                if self._last_geo != f"{cw}x{ch}+{cx}+{cy}":
                    self.overlay.geometry(f"{cw}x{ch}+{cx}+{cy}")
                    # 如果窗口大小变了，需要重新渲染内容以适配缩放
                    if self._last_geo != "":
                        self._render_settlement_content(cw, ch)
                    self._last_geo = f"{cw}x{ch}+{cx}+{cy}"
            except (TclError, Exception):
                pass
        self.root.after(100, self._settlement_sync_loop)

    def show_custom_settlement_box(self):
        """ 弹出成绩结算遮罩窗口 """
        if self.settlement_active:
            return
        self.settlement_active = True
        cw, ch, cx, cy = 256, 224, 0, 0
        if not self.overlay:
            try:
                self.overlay = Toplevel(self.root)
                self.overlay.overrideredirect(True)
                self.overlay.attributes("-topmost", True)
                self.canvas = Canvas(self.overlay, bg="black", highlightthickness=0)
                self.canvas.pack(fill="both", expand=True)
            except TclError:
                self.settlement_active = False
                return
        m = self._get_mesen_window()
        if not m:
            self.close_overlay()
            return
        hwnd = getattr(m, '_hWnd', None)
        if hwnd:
            cw, ch, cx, cy = self._get_client_geometry(hwnd)
        self._last_geo = f"{cw}x{ch}+{cx}+{cy}"
        self.overlay.geometry(self._last_geo)
        self._render_settlement_content(cw, ch)
        self._settlement_sync_loop()

    def _render_settlement_content(self, cw, ch):
        """ 绘制美化版的成绩单：包括背景图、数据文字和动态的三角力量 """
        try:
            self.canvas.delete("all")
            tf_val = self.update_settlement_display()
            rom_chapter = self.read_lua_file("chapter_signal.txt")
            final_ch = str(self.selected_chapter)
            if rom_chapter and rom_chapter != "FF":
                match = re.search(r'(\d+)', rom_chapter)
                if match:
                    final_ch = match.group(1)

            # 1. 绘制背景图
            bg_path = os.path.join("ui", "bg_result.png")
            if os.path.exists(bg_path):
                bg_img = Image.open(bg_path).resize((cw, ch), Image.Resampling.LANCZOS)
                self.bg_image_ref = ImageTk.PhotoImage(bg_img)
                self.canvas.create_image(0, 0, anchor="nw", image=self.bg_image_ref)

            # 2. 绘制标题文字
            f_size = int(ch * 0.045)
            self.canvas.create_text(cw / 2, ch * 0.12, text="BS 塞尔达传说成绩", fill="#FFFFFF",
                                    font=("Verdana", f_size))
            self.canvas.create_text(cw / 2, ch * 0.20, text=f"— 第 {final_ch} 周 —", fill="#FFFFFF",
                                    font=("Verdana", f_size))

            # 3. 绘制核心数据行
            label_x, value_x, curr_y, spacing = cw * 0.15, cw * 0.42, ch * 0.30, ch * 0.09
            self.canvas.create_text(label_x, curr_y, text=self.ganon_var.get(), fill="#FFFFFF",
                                    font=("Verdana", f_size), anchor="w")
            curr_y += spacing
            self.canvas.create_text(label_x, curr_y, text="三角力量", fill="#FFFFFF", font=("Verdana", f_size),
                                    anchor="w")

            # 4. 绘制动态三角力量图标
            tf_size_val = int(cw * 0.055)
            self.triforce_frames = self._load_gif_frames(
                os.path.join("ui", "triforce_on.gif"), (tf_size_val, tf_size_val))
            off_path = os.path.join("ui", "triforce_off.png")

            # 解析 8 位二进制位，对应 8 个碎片
            tf_bits = [int(b) for b in bin(tf_val)[2:].zfill(8)]
            for i, bit in enumerate(tf_bits):
                cur_x = (value_x + tf_size_val / 2) + (i * (tf_size_val + int(cw * 0.01)))
                if bit == 1 and self.triforce_frames:
                    # 拥有碎片：绘制 GIF 帧并开启循环动画
                    img_id = self.canvas.create_image(cur_x, curr_y, image=self.triforce_frames[0], anchor="center")
                    self._animate_triforce(img_id, 0)
                elif os.path.exists(off_path):
                    # 未拥有碎片：绘制灰色静态图片
                    off_img = ImageTk.PhotoImage(
                        Image.open(off_path).resize((tf_size_val, tf_size_val), Image.Resampling.LANCZOS))
                    self.canvas.create_image(cur_x, curr_y, image=off_img, anchor="center")
                    self.ui_refs.append(off_img)

            # 5. 绘制其他统计项
            labels = ["重新开始的次数", "损失的心心数量", "所持的卢比数量"]
            vals = [self.death_var.get().split(":")[-1].strip(), self.heart_var.get().split(":")[-1].strip(),
                    self.rupee_var.get().split(":")[-1].strip()]
            for i in range(3):
                curr_y += spacing
                self.canvas.create_text(label_x, curr_y, text=labels[i], fill="#FFFFFF", font=("Verdana", f_size),
                                        anchor="w")
                self.canvas.create_text(cw * 0.85, curr_y, text=vals[i], fill="#FFFFFF", font=("Verdana", f_size),
                                        anchor="e")

            # 6. 交互提示
            self.canvas.create_rectangle(cw * 0.1, ch * 0.85, cw * 0.9, ch * 0.93, outline="#F1C40F", width=3)
            self.canvas.create_text(cw / 2, ch * 0.89, text="点击屏幕查看下一页", fill="#FFFFFF",
                                    font=("Verdana", f_size))
            # 绑定左键点击事件，进入最终结局视频
            self.canvas.bind("<Button-1>", lambda _event: self.play_ending_video())
        except TclError:
            pass
        except (AttributeError, FileNotFoundError, OSError) as data_err:
            logging.error(f"渲染数据加载失败: {data_err}")
        except Exception as unknown_err:
            logging.error(f"未预期的渲染异常: {unknown_err}")

    def _animate_triforce(self, img_id, frame_idx):
        """ 递归调用实现 GIF 帧切换，实现三角力量的动态效果 """
        if not self.overlay or not self.settlement_active:
            return
        try:
            idx = (frame_idx + 1) % len(self.triforce_frames)
            self.canvas.itemconfig(img_id, image=self.triforce_frames[idx])
            self.root.after(100, lambda: self._animate_triforce(img_id, idx))
        except (TclError, Exception):
            pass

    def play_ending_video(self):
        """ 播放结局视频逻辑（当成绩单被点击后触发） """
        self.settlement_active = False
        rom_chapter = self.read_lua_file("chapter_signal.txt")
        final_ch = str(self.selected_chapter)
        if rom_chapter and rom_chapter != "FF":
            match = re.search(r'(\d+)', rom_chapter)
            if match:
                final_ch = match.group(1)
        video_p = os.path.join(self.mesen_dir, "bszelda", "video", f"ED{final_ch}.mp4")
        if os.path.exists(video_p):
            if self.cap:
                self.cap.release()
            self.cap, self.is_ending_mode, self.image_ptr = cv2.VideoCapture(video_p), True, None
            try:
                self.canvas.delete("all")
                self.canvas.unbind("<Button-1>")  # 解除点击绑定
            except (TclError, Exception):
                pass
            logging.info(f"[结局视频] 成功播放: {os.path.basename(video_p)}")
            self._render_loop()
        else:
            logging.warning(f"[结局视频] 未找到文件 {os.path.basename(video_p)}，直接关闭遮罩。")
            self.close_overlay()

    def run(self):
        """ 运行 Tkinter 事件循环 """
        self.root.mainloop()


if __name__ == "__main__":
    # 程序入口：实例化对象并运行
    BSXSimulator().run()
