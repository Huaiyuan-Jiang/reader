"""书架这一侧（代理 A）几个界面共用的小零件：图标、封面、进度条、周柱、标签、卡片、日期说法。

只给 shelf / start_dialog / login / notes_view 用。颜色一律从 theme 取，换主题自动重画。
尺寸照界面稿 docs/design/index.html 的 CSS（.btn / .tag / .cover / .pbar / .week …）。
"""

from __future__ import annotations

import hashlib
import weakref
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QWidget)

from reader.ui import theme

# ── 图标（界面稿里的线条图标，16×16 视框，描边 1.5） ────────────────
_ICONS = {
    "plus": '<path d="M8 3v10M3 8h10"/>',
    "ok": '<path d="M3 8.5l3 3 7-7"/>',
    "x": '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    "copy": '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M3 10.5V3.5a1 1 0 011-1h6.5"/>',
    "term": '<rect x="1.5" y="2.5" width="13" height="11" rx="2"/><path d="M4.5 6.5l2 1.75-2 1.75M8.5 10.5h3"/>',
    "file": '<path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9 1.5V5.5h4"/>',
    "dl": '<path d="M8 2.5v8M4.5 7L8 10.5 11.5 7M2.5 13.5h11"/>',
    "spin": '<path d="M8 2a6 6 0 105.2 3"/>',
    "folder": '<path d="M1.5 4a1 1 0 011-1h3.5l1.5 1.5h6a1 1 0 011 1V12a1 1 0 01-1 1h-11a1 1 0 01-1-1z"/>',
    "back": '<path d="M6 4L2.5 7.5 6 11M2.5 7.5h7a3.5 3.5 0 010 7H8"/>',
    "r": '<path d="M6 3.5L10.5 8 6 12.5"/>',
}


def _svg(name: str, color: QColor, stroke: float = 1.5) -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
            f'stroke="{color.name()}" stroke-opacity="{color.alphaF():.3f}" stroke-width="{stroke}" '
            f'stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>').encode()


def icon_pixmap(name: str, color: QColor | str, px: int = 16, dpr: float = 2.0) -> QPixmap:
    c = theme.color(color) if isinstance(color, str) else color
    r = QSvgRenderer(QByteArray(_svg(name, c)))
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def icon(name: str, color: str = "t1", px: int = 16) -> QIcon:
    return QIcon(icon_pixmap(name, color, px))


# ── 换主题时回调（弱引用，控件删了就不管） ──────────────────────
def on_theme(widget: QWidget, fn: Callable[[], None]) -> None:
    ref = weakref.ref(widget)

    def _cb(_name: str) -> None:
        w = ref()
        if w is None:
            return
        try:
            fn()
        except RuntimeError:     # C++ 对象已删
            pass
    theme.on_change(_cb)


def repaint_on_theme(widget: QWidget) -> None:
    on_theme(widget, widget.update)


# ── 文字 ────────────────────────────────────────────────────
class Label(QLabel):
    """一行字：字号、字重、颜色键名。换主题自动变色。"""

    def __init__(self, text: str = "", px: int = 14, weight: int = 400, color: str = "t1",
                 *, tnum: bool = False, wrap: bool = False, spacing: float = 0.0,
                 parent: QWidget | None = None, selectable: bool = False):
        super().__init__(text, parent)
        f = theme.font(px, weight, tnum=tnum)
        if spacing:
            f.setLetterSpacing(f.SpacingType.AbsoluteSpacing, spacing)
        self.setFont(f)
        self._color = color
        self.setWordWrap(wrap)
        if selectable:
            self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._restyle()
        on_theme(self, self._restyle)

    def set_color(self, key: str) -> None:
        self._color = key
        self._restyle()

    def _restyle(self) -> None:
        c = self._color if self._color.startswith("#") else theme.tok(self._color)
        self.setStyleSheet(f"QLabel {{ color: {c}; background: transparent; }}")


class ElideLabel(Label):
    """放不下就在末尾省略成「…」，完整的字在悬停提示里。"""

    def __init__(self, text: str = "", *a, **kw):
        super().__init__("", *a, **kw)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        fm = self.fontMetrics()
        shown = fm.elidedText(self._full, Qt.ElideRight, max(10, self.width()))
        QLabel.setText(self, shown)
        if shown != self._full and not self.toolTip():
            self.setToolTip(self._full)

    def sizeHint(self):
        sh = super().sizeHint()
        sh.setWidth(self.fontMetrics().horizontalAdvance(self._full) + 2)
        return sh


def lbl(text: str) -> Label:
    """界面稿的 .lbl：小号、淡色、字距开一点的栏目名。"""
    return Label(text, 12, 400, "t4", spacing=0.7)


def cap(text: str = "", color: str = "t4", *, wrap: bool = False) -> Label:
    return Label(text, 12, 400, color, tnum=True, wrap=wrap)


def rich(text: str, px: int = 13, color: str = "t2", strong: str = "t1", *, wrap: bool = True,
         weight: int = 400, line: float = 1.7) -> QLabel:
    """带 <b> 的一段话（<b> 用更亮的颜色）。"""
    w = QLabel()
    w.setWordWrap(wrap)
    w.setTextFormat(Qt.RichText)
    w.setFont(theme.font(px, weight))

    def _set() -> None:
        body = text.replace("<b>", f'<span style="color:{theme.tok(strong)}; font-weight:600">') \
                   .replace("</b>", "</span>")
        # Qt 的比例行高乘在字体自然行距上（MiSans 约 1.33），CSS 的行高要换算
        w.setText(f'<div style="line-height:{int(line / theme.MISANS_NATURAL * 100)}%">{body}</div>')
        w.setStyleSheet(f"QLabel {{ color: {theme.tok(color)}; background: transparent; }}")
    _set()
    on_theme(w, _set)
    return w


# ── 按钮 ────────────────────────────────────────────────────
_SIZES = {
    "": "height: 34px; padding: 0 14px; border-radius: 8px; font-size: 13px;",
    "sm": "height: 28px; padding: 0 10px; border-radius: 6px; font-size: 12px;",
    "lg": "height: 44px; padding: 0 24px; border-radius: 10px; font-size: 15px;",
}


class Button(QPushButton):
    """界面稿的 .btn：kind = "" / "pri" / "ghost" / "danger"；size = "" / "sm" / "lg"。"""

    def __init__(self, text: str = "", kind: str = "", size: str = "", icon_name: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self._kind, self._size, self._icon = kind, size, icon_name
        if kind:
            self.setProperty("kind", kind)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(theme.font({"sm": 12, "lg": 15}.get(size, 13), 600 if kind == "pri" else 400))
        self._restyle()
        on_theme(self, self._restyle)

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.setProperty("kind", kind or None)
        self.setFont(theme.font(self.font().pixelSize(), 600 if kind == "pri" else 400))
        self.style().unpolish(self)
        self.style().polish(self)
        self._restyle()

    def set_icon_name(self, name: str | None) -> None:
        self._icon = name
        self._restyle()

    def _restyle(self) -> None:
        sz = _SIZES[self._size]
        t = theme.THEMES[theme.current()]
        extra = ""
        if self._kind == "pri":
            extra = (f"QPushButton:hover {{ background: {theme.mix('pri', 'bg3', 0.88)}; }}"
                     f"QPushButton:disabled {{ background: {t['bg4']}; border-color: {t['bg4']}; color: {t['t5']}; }}")
        elif self._kind == "danger":
            extra = (f"QPushButton {{ background: {theme.mix('red', 'bg3', 0.13)}; "
                     f"border-color: {theme.mix('red', 'bg3', 0.55)}; }}")
        # 键盘焦点给一圈边框；主按钮不加（窗口一打开焦点就在它上面，一直带圈反而像出了什么事）
        ring = "" if self._kind == "pri" else f"QPushButton:focus {{ border-color: {t['focus']}; }}"
        self.setStyleSheet(f"QPushButton {{ {sz} }}" + ring + extra)
        if self._icon:
            key = {"pri": "pri_ink", "ghost": "t3"}.get(self._kind, "t1")
            px = 14 if self._size == "sm" else 16
            self.setIcon(icon(self._icon, key, px))
            self.setIconSize(QSize(px, px))


# ── 卡片 / 框 ────────────────────────────────────────────────
ColorSpec = str | Callable[[], QColor] | None


def _qc(spec: ColorSpec) -> QColor | None:
    if spec is None:
        return None
    if callable(spec):
        return spec()
    return theme.color(spec)


class Card(QFrame):
    """自己画的圆角块：底色、描边（可虚线）、阴影。颜色是键名或返回 QColor 的函数。"""

    def __init__(self, bg: ColorSpec = "bg3", border: ColorSpec = "l1", radius: float = 14,
                 *, border_w: float = 1.0, dashed: bool = False, shadow: tuple[int, int] | None = None,
                 radii: tuple[float, float, float, float] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.bg, self.border, self.radius = bg, border, radius
        self.border_w, self.dashed, self.radii = border_w, dashed, radii
        self.setAttribute(Qt.WA_StyledBackground, False)
        if shadow:
            eff = QGraphicsDropShadowEffect(self)
            eff.setOffset(0, shadow[0])
            eff.setBlurRadius(shadow[1])
            self._shadow = eff
            self._apply_shadow()
            self.setGraphicsEffect(eff)
            on_theme(self, self._apply_shadow)
        repaint_on_theme(self)

    def _apply_shadow(self) -> None:
        self._shadow.setColor(theme.color("shadow"))

    def _path(self, r: QRectF) -> QPainterPath:
        path = QPainterPath()
        if self.radii:
            tl, tr, br, bl = self.radii
            path.moveTo(r.left() + tl, r.top())
            path.lineTo(r.right() - tr, r.top())
            path.arcTo(r.right() - 2 * tr, r.top(), 2 * tr, 2 * tr, 90, -90) if tr else path.lineTo(r.right(), r.top())
            path.lineTo(r.right(), r.bottom() - br)
            path.arcTo(r.right() - 2 * br, r.bottom() - 2 * br, 2 * br, 2 * br, 0, -90) if br else path.lineTo(r.right(), r.bottom())
            path.lineTo(r.left() + bl, r.bottom())
            path.arcTo(r.left(), r.bottom() - 2 * bl, 2 * bl, 2 * bl, -90, -90) if bl else path.lineTo(r.left(), r.bottom())
            path.lineTo(r.left(), r.top() + tl)
            path.arcTo(r.left(), r.top(), 2 * tl, 2 * tl, 180, -90) if tl else path.lineTo(r.left(), r.top())
            path.closeSubpath()
        else:
            path.addRoundedRect(r, self.radius, self.radius)
        return path

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bw = self.border_w if self.border is not None else 0
        r = QRectF(self.rect()).adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2)
        path = self._path(r)
        bg = _qc(self.bg)
        if bg is not None:
            p.fillPath(path, bg)
        bc = _qc(self.border)
        if bc is not None and bw:
            pen = QPen(bc, bw)
            if self.dashed:
                pen.setStyle(Qt.CustomDashLine)
                pen.setDashPattern([4, 3])
            p.setPen(pen)
            p.drawPath(path)
        p.end()


class Tag(QLabel):
    """界面稿的 .tag：小灰底标签（EPUB / PDF / 扫描版）。"""

    def __init__(self, text: str, color: str = "t3", bg: str = "bg4", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setFont(theme.font(11, 400))
        self.setFixedHeight(20)
        self._c, self._bg = color, bg
        self._restyle()
        on_theme(self, self._restyle)

    def _restyle(self) -> None:
        self.setStyleSheet(f"QLabel {{ background: {theme.tok(self._bg)}; color: {theme.tok(self._c)}; "
                           f"border-radius: 5px; padding: 0 7px; }}")


class PBar(QWidget):
    """细进度条（界面稿 .pbar，高 4）。"""

    def __init__(self, value: float = 0.0, fill: str = "acc", track: str = "bg5", h: int = 4,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.value, self.fill, self.track = value, fill, track
        self.setFixedHeight(h)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        repaint_on_theme(self)

    def set_value(self, v: float, fill: str | None = None) -> None:
        self.value = max(0.0, min(1.0, v))
        if fill:
            self.fill = fill
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        r = QRectF(0, 0, self.width(), h)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.color(self.track))
        p.drawRoundedRect(r, h / 2, h / 2)
        if self.value > 0:
            p.setBrush(theme.color(self.fill))
            p.drawRoundedRect(QRectF(0, 0, max(h, self.width() * self.value), h), h / 2, h / 2)
        p.end()


class Dot(QWidget):
    def __init__(self, color: str = "green", d: int = 7, parent: QWidget | None = None):
        super().__init__(parent)
        self.color_key = color
        self.setFixedSize(d, d)
        repaint_on_theme(self)

    def set_color(self, key: str) -> None:
        self.color_key = key
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.color(self.color_key))
        p.drawEllipse(self.rect())
        p.end()


class IconLabel(QLabel):
    def __init__(self, name: str, color: str = "t4", px: int = 16, parent: QWidget | None = None):
        super().__init__(parent)
        self._name, self._color, self._px = name, color, px
        self.setFixedSize(px, px)
        self._redo()
        on_theme(self, self._redo)

    def set_icon(self, name: str, color: str | None = None) -> None:
        self._name = name
        if color:
            self._color = color
        self._redo()

    def _redo(self) -> None:
        self.setPixmap(icon_pixmap(self._name, self._color, self._px))


class Badge(QWidget):
    """红底白字的圆形「!」（登录卡）。"""

    def __init__(self, d: int = 20, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(d, d)
        repaint_on_theme(self)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.color("red"))
        p.drawEllipse(self.rect())
        p.setPen(theme.color("acc_ink"))
        p.setFont(theme.font(int(self.height() * 0.6), 700))
        p.drawText(self.rect(), Qt.AlignCenter, "!")
        p.end()


class Seg(QFrame):
    """两段的切换（亮 / 暗）。value_changed 发选中的键。"""

    value_changed = Signal(str)

    def __init__(self, items: list[tuple[str, str]], value: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._btns: dict[str, QPushButton] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        for key, text in items:
            b = QPushButton(text, self)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFont(theme.font(12, 400))
            b.clicked.connect(lambda _=False, k=key: self.set_value(k, emit=True))
            lay.addWidget(b)
            self._btns[key] = b
        self.setObjectName("seg")
        self.set_value(value)
        self._restyle()
        on_theme(self, self._restyle)

    def value(self) -> str:
        return next((k for k, b in self._btns.items() if b.isChecked()), "")

    def set_value(self, key: str, emit: bool = False) -> None:
        for k, b in self._btns.items():
            b.setChecked(k == key)
            b.setFont(theme.font(12, 600 if k == key else 400))
        if emit:
            self.value_changed.emit(key)

    def _restyle(self) -> None:
        t = theme.THEMES[theme.current()]
        self.setStyleSheet(
            f"#seg {{ background: {t['bg4']}; border-radius: 7px; }}"
            f"#seg QPushButton {{ height: 24px; padding: 0 10px; border-radius: 5px; border: 0; "
            f"background: transparent; color: {t['t3']}; font-size: 12px; }}"
            f"#seg QPushButton:hover {{ color: {t['t1']}; }}"
            f"#seg QPushButton:checked {{ background: {t['bg2']}; color: {t['t1']}; }}"
            f"#seg QPushButton:focus {{ border: 1px solid {t['focus']}; }}")


# ── 封面 ────────────────────────────────────────────────────
def cover_color(title: str) -> QColor:
    """没有封面图时的素色书皮：按书名算一个低饱和的深色（同一本书永远同一个颜色）。"""
    h = int(hashlib.md5(title.encode("utf-8")).hexdigest()[:4], 16) / 0xFFFF
    return QColor.fromHslF(h, 0.22, 0.30)


class Cover(QWidget):
    """书皮：有封面图就用图（裁满），没有就画素色书皮 + 竖排书名。左边一道深色书脊。"""

    def __init__(self, title: str, cover: str = "", w: int = 32, h: int = 44, font_px: int = 0,
                 *, shadow: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.title, self.font_px = title, font_px
        self.setFixedSize(w, h)
        self._pm: QPixmap | None = None
        if cover and Path(cover).is_file():
            pm = QPixmap(cover)
            if not pm.isNull():
                self._pm = pm
        if shadow:
            eff = QGraphicsDropShadowEffect(self)
            eff.setOffset(0, 4 if h > 100 else 2)
            eff.setBlurRadius(14 if h > 100 else 6)
            eff.setColor(theme.color("shadow"))
            self.setGraphicsEffect(eff)
            on_theme(self, lambda: eff.setColor(theme.color("shadow")))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        path = QPainterPath()
        rr = 3 if w < 60 else 6
        path.addRoundedRect(QRectF(0, 0, w, h), rr, rr)
        p.setClipPath(path)
        if self._pm is not None:
            pm = self._pm
            dpr = self.devicePixelRatioF()
            scaled = pm.scaled(QSize(int(w * dpr), int(h * dpr)), Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            sx = (scaled.width() / dpr - w) / 2
            sy = (scaled.height() / dpr - h) / 2
            p.drawPixmap(QRectF(-sx, -sy, scaled.width() / dpr, scaled.height() / dpr), scaled,
                         QRectF(scaled.rect()))
        else:
            p.fillRect(self.rect(), cover_color(self.title))
            if self.font_px:
                self._vertical_title(p, w, h)
        spine = QColor(0, 0, 0)
        spine.setAlphaF(0.18)
        p.fillRect(QRectF(0, 0, 3 if w > 40 else 2, h), spine)
        p.end()

    def _vertical_title(self, p: QPainter, w: int, h: int) -> None:
        """竖排书名（从右往左一列列排），只画得下的部分。"""
        ink = theme.color("paper", theme="light")      # 书皮上的字：暖白，两种主题一样
        fpx = self.font_px
        p.setPen(ink)
        p.setFont(theme.font(fpx, 600))
        chars = [c for c in self.title.replace("《", "").replace("》", "") if not c.isspace()]
        step = int(fpx * 1.25)
        per_col = max(1, (h - 24) // step)
        x = w - 12 - fpx
        i = 0
        while i < len(chars) and x > 8:
            y = 12
            for c in chars[i:i + per_col]:
                p.drawText(QRectF(x, y, fpx + 2, step), Qt.AlignHCenter | Qt.AlignTop, c)
                y += step
            i += per_col
            x -= int(fpx * 1.4)


# ── 这周七根柱子 ────────────────────────────────────────────
class WeekBars(QWidget):
    """界面稿 .week：七根柱子（满 45 的颜色实一些），下面是星期几，今天加粗。"""

    NAMES = "一二三四五六日"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.values: list[float] = [0.0] * 7
        self.today = -1
        self.setFixedHeight(34 + 4 + 16)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        repaint_on_theme(self)

    def set_week(self, values: list[float], today: int) -> None:
        self.values, self.today = values, today
        tips = [f"周{self.NAMES[i]}：{round(v * 45)} 分钟" for i, v in enumerate(values)]
        self.setToolTip("\n".join(tips[:today + 1]) if today >= 0 else "")
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        gap = 6
        cw = (self.width() - gap * 6) / 7
        for i in range(7):
            x = i * (cw + gap)
            bar = QRectF(x, 0, cw, 34)
            clip = QPainterPath()
            clip.addRoundedRect(bar, 4, 4)
            p.fillPath(clip, theme.color("bg4"))
            v = max(0.0, min(1.0, self.values[i] if i < len(self.values) else 0))
            if v > 0:
                full = v >= 1.0
                c = theme.color("acc") if full else QColor(theme.mix("acc", "bg4", 0.55))
                p.save()
                p.setClipPath(clip)
                p.fillRect(QRectF(x, 34 * (1 - v), cw, 34 * v), c)
                p.restore()
            is_today = i == self.today
            p.setPen(theme.color("t1" if is_today else "t4"))
            p.setFont(theme.font(11, 600 if is_today else 400))
            p.drawText(QRectF(x, 38, cw, 16), Qt.AlignHCenter | Qt.AlignTop, self.NAMES[i])
        p.end()


# ── 杂项 ────────────────────────────────────────────────────
def hline(color: str = "l1") -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {theme.tok(color)}; border: 0;")
    on_theme(f, lambda: f.setStyleSheet(f"background: {theme.tok(color)}; border: 0;"))
    return f


def row(*widgets, spacing: int = 8, stretch_at: int | None = None, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for i, w in enumerate(widgets):
        if stretch_at == i:
            lay.addStretch(1)
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, int):
            lay.addSpacing(w)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    return lay


WEEKDAYS = "一二三四五六日"


def md(d: date) -> str:
    return f"{d.month} 月 {d.day} 日"


def day_header(d: date) -> str:
    """「9 月 24 日 周四」"""
    return f"{md(d)} 周{WEEKDAYS[d.weekday()]}"


def when_label(ts: str | datetime | None, now: datetime | None = None, *, with_time: bool = True) -> str:
    """「今天 08:58」「昨天 21:10」「9 月 19 日」（今年以前带年份）。"""
    if not ts:
        return ""
    try:
        dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except ValueError:
        return ""
    now = now or datetime.now()
    days = (now.date() - dt.date()).days
    hm = dt.strftime("%H:%M")
    if days == 0:
        return f"今天 {hm}" if with_time else "今天"
    if days == 1:
        return f"昨天 {hm}" if with_time else "昨天"
    if dt.year != now.year:
        return f"{dt.year} 年 {md(dt.date())}"
    return md(dt.date())


def part_of_day(dt: datetime) -> str:
    h = dt.hour
    return "凌晨" if h < 5 else "早上" if h < 11 else "中午" if h < 13 else "下午" if h < 18 else "晚上"


# ── 放到后台线程做（界面线程不等） ─────────────────────────────
from PySide6.QtCore import QObject, QRunnable, QThreadPool  # noqa: E402


class _Relay(QObject):
    done = Signal(object, object)     # (结果, 异常)


class _Job(QRunnable):
    def __init__(self, fn, relay: _Relay):
        super().__init__()
        self.fn, self.relay = fn, relay
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            res, err = self.fn(), None
        except BaseException as e:  # noqa: BLE001
            res, err = None, e
        self.relay.done.emit(res, err)


_live: set[_Relay] = set()


def background(fn: Callable[[], object], on_done: Callable[[object, BaseException | None], None],
               pool: QThreadPool | None = None) -> None:
    """fn 在线程池里跑；跑完在界面线程调 on_done(结果, 异常)。"""
    relay = _Relay()
    _live.add(relay)

    def _finish(res, err):
        _live.discard(relay)
        relay.deleteLater()
        on_done(res, err)
    relay.done.connect(_finish, Qt.QueuedConnection)
    (pool or QThreadPool.globalInstance()).start(_Job(fn, relay))
