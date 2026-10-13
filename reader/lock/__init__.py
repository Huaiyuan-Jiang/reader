"""reader 锁屏核心（中等强度，纯 Qt 窗口 + 纯逻辑控制器）。

范围（2026-09-24 定的缩小版）：
- 不装全局键盘钩子、不抢前台、不看门重启、不干预别的程序。
- 靠一个无边框全屏置顶窗口：会话期间别的程序切不到前面来，但都在后台照常跑。

导入本包只拉纯逻辑（不依赖 Qt），方便离线单测。要用窗口件再单独
``from reader.lock.windows import ReadingWindow`` —— 那个模块才依赖 PySide6。
"""

from .emergency import EmergencyExit
from .session import LockSession
from .signal import Signal
from .state import (
    ACTIVE, STATUS_PATH, EMERGENCY_EXIT, FINISHED, FORCED_QUIT, SESSION_STATE_PATH,
    STATE_DIR, DailySummary, SessionState, StatusStore,
)
from .timer import ReadingTimer

__all__ = [
    "LockSession", "ReadingTimer", "EmergencyExit", "Signal",
    "SessionState", "StatusStore", "DailySummary",
    "STATE_DIR", "SESSION_STATE_PATH", "STATUS_PATH",
    "ACTIVE", "FINISHED", "EMERGENCY_EXIT", "FORCED_QUIT",
]
