"""锁屏用到的 Qt 窗口件 —— 全是普通 Qt 特性，不装钩子、不抢焦点、不动别的程序。

主人要的效果：读书时别的程序照常在后台跑，但一个会话里主人没法把它们切到前面来。
这里靠的就是一个无边框、全屏、置顶（Qt.WindowStaysOnTopHint）的窗口：后开的普通窗口也压在它下面。
我们**不**用代码去抢前台 —— 只在显示时和主人点它时把自己抬一下（raise_）。
真要走开（紧急出口 / 计时结束），窗口正常关掉即可。

三个东西：
- ReadingWindow：主屏上那个读书窗口。
- OverlayManager：其他屏幕铺深色遮罩，文字可更新（「正在读书，还剩 N 分钟」），
  跟随屏幕插拔（QGuiApplication.screenAdded / screenRemoved）。
- LeaveWatcher：应用失去 / 得到激活时发 left / returned 信号，交给 LockSession 暂停计时、记一次离开。
  只报告，不自动抢回来。

本模块要 Qt，只有真界面和手动 demo 用，逻辑单测不会导入它。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

# 无边框 + 置顶。Tool 类型不进任务栏。
_LOCK_FLAGS = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint


def _fill_screen(widget: QWidget, screen: QScreen) -> None:
    widget.setGeometry(screen.geometry())


class ReadingWindow(QWidget):
    """主屏上的读书窗口：无边框、全屏、置顶。放内容用 setContentWidget。

    只在 showEvent 和被点击时抬升自己，绝不用 SetForegroundWindow 之类去抢焦点。
    """

    def __init__(self, screen: QScreen | None = None, parent: QWidget | None = None):
        super().__init__(parent, _LOCK_FLAGS)
        self._screen = screen or QGuiApplication.primaryScreen()
        self.setObjectName("ReadingWindow")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._content: QWidget | None = None

    def set_content_widget(self, widget: QWidget) -> None:
        if self._content is not None:
            self._layout.removeWidget(self._content)
            self._content.setParent(None)
        self._content = widget
        self._layout.addWidget(widget)

    def show_locked(self) -> None:
        if self._screen is not None:
            self.setScreen(self._screen)
            _fill_screen(self, self._screen)
        self.showFullScreen()
        self.raise_()

    def showEvent(self, event):  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self.raise_()

    def mousePressEvent(self, event):  # noqa: N802
        # 主人点了我，就把我抬到最前（不是抢，是响应主人操作）
        self.raise_()
        super().mousePressEvent(event)


class OverlayManager(QObject):
    """给每个非主屏铺一层深色遮罩，文字可更新，跟随屏幕插拔。"""

    def __init__(self, text: str = "正在读书", parent: QObject | None = None):
        super().__init__(parent)
        self._text = text
        self._covers: dict[QScreen, _OverlayCover] = {}

    def show(self) -> None:
        app = QGuiApplication.instance()
        app.screenAdded.connect(self._on_screen_added)
        app.screenRemoved.connect(self._on_screen_removed)
        for screen in QGuiApplication.screens():
            self._ensure_cover(screen)

    def set_text(self, text: str) -> None:
        self._text = text
        for cover in self._covers.values():
            cover.set_text(text)

    def close(self) -> None:
        app = QGuiApplication.instance()
        try:
            app.screenAdded.disconnect(self._on_screen_added)
            app.screenRemoved.disconnect(self._on_screen_removed)
        except (RuntimeError, TypeError):
            pass
        for cover in list(self._covers.values()):
            cover.close()
        self._covers.clear()

    # 主屏留给 ReadingWindow，不铺遮罩
    def _ensure_cover(self, screen: QScreen) -> None:
        if screen is QGuiApplication.primaryScreen():
            return
        if screen in self._covers:
            return
        cover = _OverlayCover(screen, self._text)
        self._covers[screen] = cover
        cover.show_on_screen()

    def _on_screen_added(self, screen: QScreen) -> None:
        self._ensure_cover(screen)

    def _on_screen_removed(self, screen: QScreen) -> None:
        cover = self._covers.pop(screen, None)
        if cover is not None:
            cover.close()


class _OverlayCover(QWidget):
    def __init__(self, screen: QScreen, text: str):
        super().__init__(None, _LOCK_FLAGS)
        self._screen = screen
        # 颜色取主题的副屏遮罩那组（两种主题都是深色）；主题模块不在就用同样的深色
        try:
            from ..ui import theme
            bg, fg = theme.tok("mon"), theme.tok("mon_t")
        except Exception:  # noqa: BLE001
            bg, fg = "#0A0A0A", "#8D8A83"
        self.setStyleSheet(f"background-color: {bg};")
        layout = QVBoxLayout(self)
        self._label = QLabel(text, self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(f"color: {fg}; font-size: 28px;")
        layout.addStretch(1)
        layout.addWidget(self._label)
        layout.addStretch(1)

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def show_on_screen(self) -> None:
        self.setScreen(self._screen)
        _fill_screen(self, self._screen)
        self.showFullScreen()
        self.raise_()


class LeaveWatcher(QObject):
    """应用失去 / 得到激活时发信号。只报告，不抢回来。

    connect 到 QGuiApplication.applicationStateChanged：切到别的程序会变成非 Active。
    我们自己的子对话框、弹窗仍属于本应用，不会触发 left（这正是要的）。
    """

    left = Signal()
    returned = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._active = True
        app = QGuiApplication.instance()
        app.applicationStateChanged.connect(self._on_state)

    def _on_state(self, state: Qt.ApplicationState) -> None:
        now_active = state == Qt.ApplicationActive
        if now_active and not self._active:
            self._active = True
            self.returned.emit()
        elif not now_active and self._active:
            self._active = False
            self.left.emit()
