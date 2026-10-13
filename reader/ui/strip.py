"""读书界面顶上那一条（界面稿板 3、6、7、10①）。

    《书名》 · 小节          这次还剩 19 分钟          今天 26 / 45 分钟 │ 紧急出口
    ────────────── 底边 2px 进度线（这次读了多少） ──────────────

- 剩余时间**只显示分钟**：余光里一直跳的秒会让人盯表。鼠标移到中间那块上，才显示到秒（「18 分 42 秒」）。
  分钟向上取整：还剩 18 分 01 秒写 19，到 0 那一刻才是真读完了。
- 状态：run 计时中 / pause 发呆暂停 / away 离开了阅读 / done 这次读够了（还没选） / extra 多读中。
  暂停时数字变灰、前面一个「暂停」小标签、进度线变灰；读够了变绿。
- 「紧急出口」淡色、放最右、**没有快捷键**，也不拿焦点：Esc、Enter、空格都碰不到它，只能用鼠标点。
  读够以后换成「随时可以离开」，点了「再读一会儿」后换成「结束」按钮。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from . import theme
from .cards import button, icon_pixmap, on_theme, restyle

STATES = ("run", "pause", "away", "done", "extra")


def minutes_left(seconds: float) -> int:
    """顶条上显示的「还剩 N 分钟」：向上取整。"""
    return max(0, math.ceil(max(0.0, seconds) / 60 - 1e-9))


def seconds_text(seconds: float) -> str:
    s = max(0, int(math.ceil(seconds - 1e-9)))
    return f"{s // 60} 分 {s % 60:02d} 秒"


class _Crumb(QWidget):
    """左边：《书名》（亮一点）· 小节（淡一点，太长就省略）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.book = ""
        self.section = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        on_theme(self, lambda w: w.update())

    def set_text(self, book: str, section: str) -> None:
        self.book, self.section = book, section
        self.setToolTip(f"{book} · {section}" if section else book)
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        fb, fs = theme.font(13, 500), theme.font(13, 400)
        mb, ms = QFontMetrics(fb), QFontMetrics(fs)
        w, h = self.width(), self.height()
        book = mb.elidedText(self.book, Qt.ElideRight, w)
        p.setFont(fb)
        p.setPen(theme.color("t3"))
        base = (h + mb.ascent() - mb.descent()) / 2
        p.drawText(0, int(base), book)
        x = mb.horizontalAdvance(book)
        if self.section and x < w - 20:
            rest = ms.elidedText(" · " + self.section, Qt.ElideRight, w - x)
            p.setFont(fs)
            p.setPen(theme.color("t4"))
            p.drawText(x, int(base), rest)


class _Clock(QWidget):
    """中间：[暂停] 这次还剩 19 分钟。鼠标移上去显示到秒。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "run"
        self.remaining = 0.0
        self.extra = 0.0
        self.hovered = False
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        on_theme(self, lambda w: w.update())
        self._ok = None

    # 片段：(文字, 字号, 字重, 颜色键, 是不是小标签)
    def _parts(self) -> list[tuple[str, int, int, str, bool]]:
        st = self.state
        if st == "done":
            return [("这次读够了", 13, 500, "greent", False)]
        if st == "extra":
            m = int(self.extra // 60)
            return [("多读了", 13, 400, "greent", False), (str(m), 17, 600, "greent", False),
                    ("分钟", 13, 400, "greent", False)]
        num_key = "t4" if st in ("pause", "away") else "t1"
        if self.hovered:
            s = max(0, int(math.ceil(self.remaining - 1e-9)))
            nums = [(str(s // 60), 17, 600, num_key, False), ("分", 13, 400, "t3", False),
                    (f"{s % 60:02d}", 17, 600, num_key, False), ("秒", 13, 400, "t3", False)]
        else:
            nums = [(str(minutes_left(self.remaining)), 17, 600, num_key, False),
                    ("分钟", 13, 400, "t3", False)]
        tag = []
        if st == "pause":
            tag = [("暂停", 11, 400, "t3", True)]
        elif st == "away":
            tag = [("暂停 · 离开了阅读", 11, 400, "t3", True)]
        return tag + [("这次还剩", 13, 400, "t3", False)] + nums

    def _layout(self):
        items, x = [], 0.0
        parts = self._parts()
        if self.state in ("done", "extra"):
            x = 22.0                       # 前面留给对勾
        for i, (text, px, wt, key, is_tag) in enumerate(parts):
            f = theme.font(px, wt, tnum=True)
            fm = QFontMetrics(f)
            tw = fm.horizontalAdvance(text)
            wbox = tw + (12 if is_tag else 0)
            items.append((x, text, f, fm, key, is_tag, wbox))
            x += wbox + (6 if i < len(parts) - 1 else 0)
            if is_tag:
                x += 4
        return items, x

    def sizeHint(self):  # noqa: N802
        _, w = self._layout()
        return QSize(int(w) + 4, 40)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        items, total = self._layout()
        x0 = (self.width() - total) / 2
        big = QFontMetrics(theme.font(17, 600))
        base = (self.height() + big.ascent() - big.descent()) / 2
        if self.state in ("done", "extra"):
            pm = icon_pixmap("ok", "greent", 16)
            p.drawPixmap(int(x0), int((self.height() - 16) / 2), pm)
        for x, text, f, fm, key, is_tag, wbox in items:
            if is_tag:
                r = QRectF(x0 + x, (self.height() - 18) / 2, wbox, 18)
                p.setPen(Qt.NoPen)
                p.setBrush(theme.color("bg4"))
                p.drawRoundedRect(r, 4, 4)
                p.setFont(f)
                p.setPen(theme.color(key))
                p.drawText(r, Qt.AlignCenter, text)
                continue
            p.setFont(f)
            p.setPen(theme.color(key))
            p.drawText(int(round(x0 + x)), int(round(base)), text)

    def enterEvent(self, e):  # noqa: N802
        self.hovered = True
        self.updateGeometry()
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802
        self.hovered = False
        self.updateGeometry()
        self.update()
        super().leaveEvent(e)


class _ExitLink(QPushButton):
    """淡色的「紧急出口」：鼠标移上去才亮一点。"""

    def set_hot(self, hot: bool) -> None:
        self.setIcon(icon_pixmap("door", "t3" if hot else "t5", 16))

    def enterEvent(self, e):  # noqa: N802
        self.set_hot(True)
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802
        self.set_hot(False)
        super().leaveEvent(e)

    def keyPressEvent(self, e):  # noqa: N802 —— 万一拿到焦点也不认键盘
        e.ignore()


class Strip(QWidget):
    exit_clicked = Signal()      # 点了「紧急出口」
    finish_clicked = Signal()    # 多读时点了「结束」

    HEIGHT = 40

    def __init__(self, parent: QWidget | None = None, target_minutes: int = 45):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.target = target_minutes
        self.state = "run"
        self.progress = 0.0

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 10, 0)
        lay.setSpacing(0)
        self.crumb = _Crumb(self)
        self.clock = _Clock(self)
        right = QWidget(self)
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(12)
        rl.addStretch(1)
        self.today = QLabel(right)
        self.today.setFont(theme.font(13, 400, tnum=True))
        self.today.setTextFormat(Qt.RichText)
        self.sep = QWidget(right)
        self.sep.setFixedSize(1, 16)
        self.sep.setAttribute(Qt.WA_StyledBackground, True)
        restyle(self.sep, lambda: f"background: {theme.tok('l2')};")
        self.exit_btn = _ExitLink("紧急出口", right)
        self.exit_btn.setObjectName("exitlink")
        self.exit_btn.setCursor(Qt.PointingHandCursor)
        self.exit_btn.setFocusPolicy(Qt.NoFocus)       # 键盘碰不到
        self.exit_btn.setAutoDefault(False)
        self.exit_btn.setToolTip("提前结束这次（要确认几次，会记下来）")
        self._exit_icon()
        on_theme(self.exit_btn, lambda _b: self._exit_icon())
        restyle(self.exit_btn, lambda: (
            f"QPushButton#exitlink {{ background: transparent; border: 0; color: {theme.tok('t5')};"
            f" font-size: 12px; padding: 4px 8px; height: 24px; border-radius: 6px; }}"
            f" QPushButton#exitlink:hover {{ background: {theme.tok('bg4')}; color: {theme.tok('t3')}; }}"))
        self.exit_btn.clicked.connect(self.exit_clicked)
        self.free = QLabel("随时可以离开", right)
        self.free.setFont(theme.font(12))
        restyle(self.free, lambda: f"color: {theme.tok('t4')}; padding: 0 8px;")
        self.finish_btn = button("结束", "pri", right)
        self.finish_btn.setFixedHeight(28)
        self.finish_btn.setToolTip("结束这次，看读书笔记")
        self.finish_btn.clicked.connect(self.finish_clicked)
        rl.addWidget(self.today)
        rl.addWidget(self.sep)
        rl.addWidget(self.exit_btn)
        rl.addWidget(self.free)
        rl.addWidget(self.finish_btn)
        # 左右等宽，中间才真正居中
        lay.addWidget(self.crumb, 1)
        lay.addWidget(self.clock, 0)
        lay.addWidget(right, 1)
        self._today_min = 0
        self._apply_state()
        on_theme(self, lambda w: (w._set_today(w._today_min), w.update()))

    def _exit_icon(self) -> None:
        self.exit_btn.set_hot(self.exit_btn.underMouse())

    # ── 对外 ─────────────────────────────────────────────
    def set_book(self, title: str, section: str = "") -> None:
        t = title if title.startswith("《") else f"《{title}》"
        self.crumb.set_text(t, section)

    def set_times(self, remaining_s: float, credited_s: float, planned_s: float,
                  today_s: float, extra_s: float = 0.0) -> None:
        self.clock.remaining = remaining_s
        self.clock.extra = extra_s
        self.progress = 1.0 if planned_s <= 0 else min(1.0, max(0.0, credited_s / planned_s))
        self._set_today(int(today_s // 60))
        self.clock.updateGeometry()
        self.clock.update()
        self.update()

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(state)
        if state != self.state:
            self.state = state
            self._apply_state()

    # ── 内部 ─────────────────────────────────────────────
    def _set_today(self, minutes: int) -> None:
        self._today_min = minutes
        self.today.setText(f'<span style="color:{theme.tok("t4")}">今天 </span>'
                           f'<span style="color:{theme.tok("t2")};font-weight:500">{minutes}</span>'
                           f'<span style="color:{theme.tok("t4")}"> / {self.target} 分钟</span>')

    def _apply_state(self) -> None:
        st = self.state
        self.clock.state = st
        locked = st in ("run", "pause", "away")
        self.exit_btn.setVisible(locked)
        self.free.setVisible(st == "done")
        self.finish_btn.setVisible(st == "extra")
        self.clock.updateGeometry()
        self.clock.update()
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, theme.color("bg1"))
        p.fillRect(0, h - 1, w, 1, theme.color("l1"))
        key = {"pause": "t5", "away": "t5", "done": "green", "extra": "green"}.get(self.state, "acc")
        frac = 1.0 if self.state in ("done", "extra") else self.progress
        p.fillRect(QRectF(0, h - 2, w * frac, 2), theme.color(key))
