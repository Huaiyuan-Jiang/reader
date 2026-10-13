"""读书界面盖在书页上的几样东西（界面稿板 6、7、10①）。

- IdleCard「还在读吗？」：5 分钟没动静时出现，书页蒙一层（蒙层由 BookView.set_dimmed 画）。
  点「在读，接着计时」= 一次活动，那 5 分钟补回来；翻页、滚一下、点书页也一样。
- LeaveCard「离开了阅读，计时停了」：焦点离开窗口时出现。不抢焦点 —— 点窗口任何地方，
  系统把焦点给回来，LeaveWatcher 报 returned，卡就走了。按钮只是给个明确的落点。
- NoticeLine：书页上方留一行（「刚才离开了 3 分 12 秒，这段没算进去，已记下」）。
  记录类的话不用会自己消失的提示条：翻页或点 × 才走。
- TimeUpBar「27 分钟读够了，随时可以离开」：时间到了不踢人，给两个选择。

规矩（界面稿「我自己定了的」）：这些卡**都不拿键盘焦点**（按钮 NoFocus、出现时不 setFocus），
←→ 翻页一直有效；不出声、不闪。颜色、字体一律从 theme 取。

也放了几件别的文件共用的小零件：图标（SvgIcon）、按主题重套样式（restyle）、取消焦点（no_focus）。
"""

from __future__ import annotations

import weakref
from typing import Callable

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from . import theme

# ── 图标（照界面稿的线条图标，currentColor 换成主题色） ─────────────
ICONS = {
    "door": '<path d="M3.5 14V2.5h6.5V14M10 4l2.5 1v9H2"/><path d="M7.5 8.5v.01"/>',
    "ok": '<path d="M3 8.5l3 3 7-7"/>',
    "x": '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    "moon": '<path d="M13 10.2A5.5 5.5 0 015.8 3a5.5 5.5 0 107.2 7.2z"/>',
    "pause": '<circle cx="8" cy="8" r="6.2"/><path d="M6.5 5.8v4.4M9.5 5.8v4.4"/>',
    "back": '<path d="M6 4L2.5 7.5 6 11M2.5 7.5h7a3.5 3.5 0 010 7H8"/>',
}


def icon_pixmap(name: str, color_key: str, size: int, dpr: float = 2.0) -> QPixmap:
    color = theme.color(color_key).name()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" stroke="{color}" '
           f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')
    px = QPixmap(QSize(round(size * dpr), round(size * dpr)))
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(0, 0, px.width(), px.height()))
    p.end()
    px.setDevicePixelRatio(dpr)
    return px


class SvgIcon(QLabel):
    """一个线条图标，换主题自动重画。"""

    def __init__(self, name: str, color_key: str = "t4", size: int = 16, parent: QWidget | None = None):
        super().__init__(parent)
        self._name, self._key, self._size = name, color_key, size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._paint()
        on_theme(self, lambda w: w._paint())

    def set_color(self, color_key: str) -> None:
        self._key = color_key
        self._paint()

    def _paint(self) -> None:
        dpr = self.devicePixelRatioF() if self.isVisible() else 2.0
        self.setPixmap(icon_pixmap(self._name, self._key, self._size, max(dpr, 2.0)))


# ── 主题小工具 ───────────────────────────────────────────────
def on_theme(widget: QWidget, fn: Callable[[QWidget], None]) -> None:
    """换主题时对 widget 调 fn(widget)；widget 删了就不再调。"""
    ref = weakref.ref(widget)

    def _again(_name: str) -> None:
        w = ref()
        if w is not None:
            try:
                fn(w)
            except RuntimeError:     # C++ 对象已删
                pass
    theme.on_change(_again)


def rgba(key: str, alpha: float | None = None) -> str:
    """主题色 → QSS 能用的 rgba(...)。"""
    c = theme.color(key, alpha)
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f})"


def restyle(widget: QWidget, make_qss: Callable[[], str]) -> None:
    """样式表由函数现算（要用 rgba / mix 时），换主题自动重算。"""
    widget.setStyleSheet(make_qss())
    on_theme(widget, lambda w: w.setStyleSheet(make_qss()))


def no_focus(*buttons: QWidget) -> None:
    """卡片上的按钮不拿键盘焦点：点了以后 ←→ 照样翻页。"""
    for b in buttons:
        b.setFocusPolicy(Qt.NoFocus)


def button(text: str, kind: str = "", parent: QWidget | None = None, *, focus: bool = False,
           height: int = 34) -> QPushButton:
    b = QPushButton(text, parent)
    if kind:
        b.setProperty("kind", kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setAutoDefault(False)
    b.setDefault(False)
    if height != 34:
        b.setFixedHeight(height)
    if not focus:
        b.setFocusPolicy(Qt.NoFocus)
    return b


def label(text: str = "", px: int = 14, color_key: str = "t2", weight: int = 400,
          parent: QWidget | None = None, *, wrap: bool = True, center: bool = False,
          tnum: bool = False, lh: float | None = None) -> QLabel:
    """一行 / 一段字。lh = 跟界面稿一样的 CSS 行高（1.75 之类），换算成 Qt 的比例行高。"""
    if lh is not None:
        pct = lh / theme.MISANS_NATURAL * 100
        text = f'<p style="margin:0;line-height:{pct:.0f}%">{text}</p>'
    lb = QLabel(text, parent)
    lb.setFont(theme.font(px, weight, tnum=tnum))
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.RichText if "<" in text else Qt.PlainText)
    if center:
        lb.setAlignment(Qt.AlignHCenter)
    restyle(lb, lambda: f"color: {theme.tok(color_key)}; background: transparent;")
    return lb


def rich(text: str) -> str:
    """把 **x** 换成加亮的粗数字（卡片里「还剩 **16** 分钟」这种）。"""
    import re
    return re.sub(r"\*\*(.+?)\*\*",
                  lambda m: f'<b style="color:{theme.tok("t1")};font-weight:600">{m.group(1)}</b>', text)


class _Floating(QFrame):
    """浮在书页上的卡：bg3 底、l2 边、圆角、投影。"""

    def __init__(self, parent: QWidget | None, radius: int = 14, border_key: str = "l2",
                 border_fn: Callable[[], str] | None = None):
        super().__init__(parent)
        self.setObjectName("floating")
        self.setAttribute(Qt.WA_StyledBackground, True)
        border = border_fn or (lambda: theme.tok(border_key))
        restyle(self, lambda: f"QFrame#floating {{ background: {theme.tok('bg3')}; "
                              f"border: 1px solid {border()}; border-radius: {radius}px; }}")
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(60)
        self._shadow.setOffset(0, 18)
        self._shadow.setColor(theme.color("shadow"))
        self.setGraphicsEffect(self._shadow)
        on_theme(self, lambda w: w._shadow.setColor(theme.color("shadow")))
        self.hide()


# ── 板 6：还在读吗 ────────────────────────────────────────────
class IdleCard(_Floating):
    resume = Signal()      # 点了「在读，接着计时」

    def __init__(self, parent: QWidget | None = None, idle_minutes: int = 5):
        super().__init__(parent)
        self.setFixedWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 28, 28, 24)
        lay.setSpacing(0)
        ic = SvgIcon("moon", "t3", 22)
        lay.addWidget(ic, 0, Qt.AlignHCenter)
        lay.addSpacing(10)
        lay.addWidget(label("还在读吗？", 20, "t1", 600, center=True))
        lay.addSpacing(8)
        lay.addWidget(label(f"{idle_minutes} 分钟没翻页，计时先停了。", 14, "t3", center=True))
        lay.addSpacing(2)
        self.credit_line = label(f"点「在读」，这 {idle_minutes} 分钟照样算。", 13, "t4", center=True)
        lay.addWidget(self.credit_line)
        lay.addSpacing(20)
        self.button = button("在读，接着计时", "pri")
        self.button.clicked.connect(self.resume)
        lay.addWidget(self.button, 0, Qt.AlignHCenter)
        lay.addSpacing(12)
        lay.addWidget(label("翻一页、滚一下也算", 12, "t4", center=True))


# ── 板 7：离开了阅读 ──────────────────────────────────────────
class LeaveCard(_Floating):
    come_back = Signal()   # 点了「回来接着读」（点窗口任何地方效果一样）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedWidth(400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 28, 30, 24)
        lay.setSpacing(0)
        lay.addWidget(SvgIcon("pause", "t3", 22), 0, Qt.AlignHCenter)
        lay.addSpacing(10)
        lay.addWidget(label("离开了阅读，计时停了", 20, "t1", 600, center=True))
        lay.addSpacing(8)
        self.left_line = label("", 14, "t2", center=True)
        lay.addWidget(self.left_line)
        lay.addSpacing(4)
        self.count_line = label("", 13, "t3", center=True)
        lay.addWidget(self.count_line)
        lay.addSpacing(20)
        self.button = button("回来接着读", "pri")
        self.button.clicked.connect(self.come_back)
        lay.addWidget(self.button, 0, Qt.AlignHCenter)
        lay.addSpacing(12)
        lay.addWidget(label("点这个窗口任何地方都行 · 别的软件在底下照常跑", 12, "t4", center=True))
        self._minutes = 0
        self._count = 0
        on_theme(self, lambda w: w.set_info(w._minutes, w._count))

    def set_info(self, minutes_left: int, leaves_today: int) -> None:
        self._minutes, self._count = minutes_left, leaves_today
        self.left_line.setTextFormat(Qt.RichText)
        self.left_line.setText(rich(f"这次还剩 **{minutes_left}** 分钟，停在这里等你。"))
        which = f"（今天第 {leaves_today} 次）" if leaves_today > 0 else ""
        self.count_line.setText(f"这次离开已经记下{which}。")


# ── 书页上方留一行 ────────────────────────────────────────────
class NoticeLine(_Floating):
    """圆角长条：图标 + 一句话 + ×。翻页或点 × 才走（调用方负责翻页时 dismiss）。"""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None, icon: str = "ok"):
        super().__init__(parent, radius=17)
        self.setFixedHeight(36)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(12)
        lay.addWidget(SvgIcon(icon, "t4", 16))
        self.text = label("", 13, "t2", wrap=False, tnum=True)
        lay.addWidget(self.text)
        self.close_btn = QPushButton()
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setToolTip("知道了")
        no_focus(self.close_btn)
        self.close_btn.setIcon(icon_pixmap("x", "t4", 14))
        self.close_btn.setIconSize(QSize(14, 14))
        on_theme(self.close_btn, lambda b: b.setIcon(icon_pixmap("x", "t4", 14)))
        restyle(self.close_btn, lambda: "QPushButton { background: transparent; border: 0; border-radius: 6px;"
                                        " padding: 0; height: 24px; }"
                                        f" QPushButton:hover {{ background: {theme.tok('bg5')}; }}")
        self.close_btn.clicked.connect(self.dismiss)
        lay.addWidget(self.close_btn)

    def show_text(self, text: str) -> None:
        self.text.setText(text)
        self.adjustSize()
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()


def away_text(seconds: float) -> str:
    """「3 分 12 秒」「45 秒」「1 小时 5 分」。"""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s // 3600} 小时 {s % 3600 // 60} 分"


def away_notice(seconds: float) -> str:
    return f"刚才离开了 {away_text(seconds)}，这段没算进去，已记下"


# ── 板 10①：时间到了 ────────────────────────────────────────
class TimeUpBar(_Floating):
    more = Signal()        # 再读一会儿
    finish = Signal()      # 结束，看读书笔记

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, radius=12,
                         border_fn=lambda: theme.mix("green", "l2", 0.45))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 12, 12)
        lay.setSpacing(14)
        self.check = SvgIcon("ok", "greent", 16)
        lay.addWidget(self.check, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = label("", 14, "t1", 600)
        self.sub = label("", 12, "t4", tnum=True)
        col.addWidget(self.title)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.more_btn = button("再读一会儿", "ghost")
        self.finish_btn = button("结束，看读书笔记", "pri")
        self.more_btn.clicked.connect(self.more)
        self.more_btn.setToolTip("接着读，多读的也算进今天")
        self.finish_btn.clicked.connect(self.finish)
        lay.addWidget(self.more_btn)
        lay.addWidget(self.finish_btn)

    def set_info(self, planned_minutes: int, today_minutes: int, target: int) -> None:
        self.title.setText(f"{planned_minutes} 分钟读够了，随时可以离开")
        if today_minutes >= target:
            self.sub.setText(f"今天 {today_minutes} / {target}，读满了。想接着读就接着读，多读的也算。")
        else:
            self.sub.setText(f"今天 {today_minutes} / {target}，还差 {target - today_minutes} 分钟。"
                             "想接着读就接着读，多读的也算。")

    def set_writing(self, error: str | None = None) -> None:
        """点了「结束」之后：在写读书笔记（不能再点），或者写不成时说一句。"""
        self.more_btn.hide()
        self.finish_btn.setEnabled(False)
        self.finish_btn.setText("AI 在写读书笔记…")
        if error:
            self.sub.setText(error)
        self.show()
        self.raise_()
