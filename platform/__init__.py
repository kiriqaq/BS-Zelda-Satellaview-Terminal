# -*- coding: utf-8 -*-
"""平台抽象层 —— 根据操作系统自动选择后端"""

import sys

_IS_MACOS = sys.platform == "darwin"
_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    from ._windows import WindowsBackend as PlatformBackend
elif _IS_MACOS:
    from ._macos import MacOSBackend as PlatformBackend
else:
    raise RuntimeError(f"不支持的操作系统: {sys.platform} (仅支持 Windows / macOS)")

IS_MACOS = _IS_MACOS
IS_WINDOWS = _IS_WINDOWS
