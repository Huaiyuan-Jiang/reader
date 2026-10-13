"""一个极小的信号对象。

为什么不直接用 PySide6 的 Signal：锁屏的控制逻辑（LockSession / 计时 / 会话状态）要能在
**不启动 Qt、不建任何窗口**的情况下单测。Qt 的 Signal 必须挂在 QObject 上，会把 Qt 拽进逻辑层。
这里用几行纯 Python 顶替，接口刻意跟 Qt 像（connect / emit），真要接到 Qt 界面上时，
界面那边 connect 一个槽函数把值转成 Qt 信号就行。
"""

from __future__ import annotations

from typing import Callable


class Signal:
    def __init__(self, name: str = ""):
        self.name = name
        self._slots: list[Callable] = []

    def connect(self, slot: Callable) -> None:
        if slot not in self._slots:
            self._slots.append(slot)

    def disconnect(self, slot: Callable) -> None:
        if slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args) -> None:
        # 复制一份再遍历：槽里可能反过来 connect/disconnect
        for slot in list(self._slots):
            slot(*args)
