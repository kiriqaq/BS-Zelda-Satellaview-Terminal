# -*- coding: utf-8 -*-
"""平台抽象基类 —— 所有平台后端必须实现的接口"""

from abc import ABC, abstractmethod


class PlatformBackend(ABC):
    """平台抽象基类，定义跨平台统一 API"""

    # ============================================================
    # 窗口管理
    # ============================================================

    @abstractmethod
    def find_window(self, title_keyword):
        """
        按标题关键字查找窗口。
        返回 dict: {name, pid, x, y, w, h, ...} 或 None
        """

    @abstractmethod
    def get_window_geometry(self, win, dpi_scale=1.0, fullscreen_optimize=False):
        """
        返回窗口客户区几何 (width, height, left, top)
        """

    @abstractmethod
    def is_minimized(self, win):
        """窗口是否处于最小化状态"""

    @abstractmethod
    def restore_window(self, win):
        """从最小化恢复窗口"""

    @abstractmethod
    def bring_to_front(self, win):
        """将窗口提到最前 / 激活"""

    @abstractmethod
    def bring_tk_to_front(self, tk_toplevel):
        """将 Tkinter 顶层窗口提到前台"""

    # ============================================================
    # 音频控制
    # ============================================================

    @property
    @abstractmethod
    def has_per_app_audio(self):
        """是否支持 per-application 音量控制"""

    @abstractmethod
    def find_audio_session(self, process_name):
        """
        缓存目标进程的音频会话句柄。
        返回可用于 mute_app() 的会话对象或 None。
        """

    @abstractmethod
    def mute_app(self, session, mute):
        """
        通过缓存的会话句柄静音/恢复应用音频。
        session: find_audio_session() 返回的对象，None 时自动重新查找。
        返回 (是否成功, 新的会话对象)
        """

    # ============================================================
    # 输入轮询（全局、非 hook 式）
    # ============================================================

    @abstractmethod
    def poll_keyboard(self):
        """轮询全局键盘状态，返回是否有任意键被按下"""

    @abstractmethod
    def poll_gamepad(self):
        """轮询游戏手柄状态，返回是否有任意键被按下"""

    # ============================================================
    # 进程与路径
    # ============================================================

    @abstractmethod
    def validate_app_path(self, user_selected_path):
        """校验用户选择的模拟器路径，返回可执行文件真实路径"""

    @abstractmethod
    def get_process_name(self):
        """返回目标模拟器的进程名（Mesen.exe 或 Mesen）"""

    @abstractmethod
    def get_settings_path(self, app_base_dir):
        """返回模拟器配置文件 settings.json 路径"""

    @abstractmethod
    def get_lua_data_dir(self, app_base_dir):
        """返回 Lua 脚本数据文件夹路径（File IPC 目录）"""

    @abstractmethod
    def get_satellaview_dir(self):
        """返回 Satellaview 广播数据目录（MesenCE BS-X 读取位置）"""

    @abstractmethod
    def get_saves_dir(self):
        """返回模拟器存档目录"""

    @abstractmethod
    def ensure_dirs(self):
        """确保所有必要目录存在"""

    @abstractmethod
    def set_app_base_dir(self, mesen_dir):
        """设置模拟器根目录（Windows 需要，macOS 忽略）"""

    @abstractmethod
    def launch_app(self, exe_path, args):
        """启动模拟器应用，返回 subprocess.Popen 对象"""

    # ============================================================
    # DPI 与系统属性
    # ============================================================

    @abstractmethod
    def get_dpi_scale(self, win=None):
        """获取系统 DPI 缩放比例（macOS 返回 1.0）"""
