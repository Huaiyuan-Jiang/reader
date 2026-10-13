"""书页的公共部分：`BookView` 接口，以及两种书页共用的零件。

零件（都是书页里面的子部件，不另开窗口 —— 锁屏时焦点不会跑到别的窗口去）：
- `BottomBar`：底栏 40px。左：目录、高亮 N；中：‹ 页码 ›、所在章和百分比；右：Aa（PDF 写「适合宽度 ▾」）。
- `Panel` / `ListPanel`：从底栏弹出的小面板（目录、高亮列表、Aa）。点外面或按 Esc 收起。
- `BackPill`：跳到原文后顶上的小条「跳到了原文 · 第 N 页　[回到刚才的第 M 页]」（界面稿板 5）。
- `Hint`：挡住下一节时书页底部那行「先答完左边这道题」。
- `DimOverlay`：发呆 / 离开时书页蒙一层（卡片由读书界面放）。不拦鼠标：点一下书页就算回来了。

规矩（界面稿「我自己定了的」）：
- 小条、面板里的按钮都不拿键盘焦点，←→ 翻页一直有效。
- 颜色全从 theme 取；换主题时跟着重画。
- 挡住提示不是会自己消失的提示条：一直留着，往回翻或者挡板换了才走。
"""

from __future__ import annotations

import difflib
import json
import time
import unicodedata
import weakref
from typing import Callable

from PySide6.QtCore import QByteArray, QEvent, QObject, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from reader.books import Book, Loc
from reader.ui import theme

# ── 图标（照抄界面稿的 SVG 路径，描边用当前颜色） ─────────────────────
_ICONS = {
    "toc": '<path d="M3 4.5h10M3 8h10M3 11.5h6.5"/>',
    "hl": '<path d="M2.5 13.5h5M5 11l6.5-6.5 2 2L7 13H5z"/>',
    "l": '<path d="M10 3.5L5.5 8l4.5 4.5"/>',
    "r": '<path d="M6 3.5L10.5 8 6 12.5"/>',
    "x": '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    "back": '<path d="M6 4L2.5 7.5 6 11M2.5 7.5h7a3.5 3.5 0 010 7H8"/>',
    "spark": '<path d="M8 1.5l1.6 4.9L14.5 8l-4.9 1.6L8 14.5l-1.6-4.9L1.5 8l4.9-1.6z"/>',
    "lock": '<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/>',
    "down": '<path d="M4.5 6.5L8 10l3.5-3.5"/>',
}


def _dpr() -> float:
    app = QApplication.instance()
    scr = app.primaryScreen() if app else None
    return scr.devicePixelRatio() if scr else 1.0


def icon(name: str, key: str = "t4", px: int = 16) -> QIcon:
    """界面稿里的线条小图标，颜色取主题的 key。"""
    c = theme.color(key)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
           f'stroke="{c.name()}" stroke-opacity="{c.alphaF():.3f}" stroke-width="1.5" '
           f'stroke-linecap="round" stroke-linejoin="round">{_ICONS[name]}</svg>')
    dpr = _dpr()
    pm = QPixmap(round(px * dpr), round(px * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


def swatch(key: str, px: int = 12, crossed: bool = False) -> QIcon:
    """高亮色的小方块（「高亮」按钮前面那个）。"""
    dpr = _dpr()
    pm = QPixmap(round(px * dpr), round(px * dpr))
    pm.fill(Qt.transparent)
    pm.setDevicePixelRatio(dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(theme.color("t4", 0.4))
    p.setBrush(theme.color(key))
    p.drawRoundedRect(QRectF(0.5, 0.5, px - 1, px - 1), 3, 3)
    if crossed:
        pen = p.pen()
        pen.setColor(theme.color("t2"))
        pen.setWidthF(1.3)
        p.setPen(pen)
        p.drawLine(2.5, px - 2.5, px - 2.5, 2.5)
    p.end()
    return QIcon(pm)


def on_theme(obj: QObject, cb: Callable[[], None]) -> None:
    """换主题时调 cb；obj 删掉后自动不再调。"""
    ref = weakref.ref(obj)

    def _again(_name: str) -> None:
        o = ref()
        if o is None:
            return
        try:
            cb()
        except RuntimeError:   # C++ 对象已删
            pass
    theme.on_change(_again)


def shadow(widget: QWidget, blur: int = 30, dy: int = 12) -> None:
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(theme.color("shadow"))
    widget.setGraphicsEffect(eff)
    on_theme(widget, lambda: eff.setColor(theme.color("shadow")))


# ── 偏好（字号、单页 / 双页 / 滚动、PDF 大小和纸色）：记住上次的选择 ─────────────
def _prefs_path():
    return theme.data_dir() / "bookview.json"


def load_prefs() -> dict:
    try:
        return json.loads(_prefs_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_prefs(**kw) -> None:
    try:
        data = load_prefs()
        data.update(kw)
        _prefs_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    except OSError:
        pass


# ── 找原文（出处标记用） ─────────────────────────────────────
def norm_chars(text: str, base: int = 0) -> tuple[str, list[int]]:
    """只留字母和数字（去掉空白、标点），小写；同时记下每个留下的字原来在哪。找原文用。"""
    out, idx = [], []
    for i, c in enumerate(text):
        cat = unicodedata.category(c)
        if cat[0] in "LN":
            lc = c.lower()
            out.append(lc if len(lc) == 1 else c)
            idx.append(base + i)
    return "".join(out), idx


def find_in(qn: str, s: str, idx: list[int], fuzzy: bool = True) -> tuple[int, int] | None:
    """在规范化后的 s 里找 qn，返回原文的 [开始, 结束)。先精确，再按最长公共片段估（至少一半对上）。"""
    if not qn or not s:
        return None
    k = s.find(qn)
    if k >= 0:
        return idx[k], idx[k + len(qn) - 1] + 1
    if not fuzzy:
        return None
    m = difflib.SequenceMatcher(None, qn, s, autojunk=False).find_longest_match(0, len(qn), 0, len(s))
    if m.size < max(4, len(qn) // 2):
        return None
    start = max(0, m.b - m.a)
    end = min(len(s), start + len(qn))
    return idx[start], idx[end - 1] + 1



# ── 零件 ─────────────────────────────────────────────────────
_BAR_BTN = """
QPushButton {{ background: transparent; border: 0; color: {t4}; font-size: 12px;
  height: 28px; padding: 0 7px; border-radius: 6px; }}
QPushButton:hover {{ background: {bg4}; color: {t2}; }}
QPushButton:checked {{ background: {bg4}; color: {t2}; }}
QPushButton:disabled {{ color: {t5}; background: transparent; }}
"""


class BarButton(QPushButton):
    """底栏上的扁平按钮：图标 + 字，不拿焦点。"""

    def __init__(self, text: str = "", icon_name: str | None = None, parent=None, px: int = 12):
        super().__init__(text, parent)
        self._icon = icon_name
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        f = theme.font(px)
        self.setFont(f)
        self.setIconSize(QSize(16, 16))
        theme.style(self, _BAR_BTN)
        self._reicon()
        on_theme(self, self._reicon)

    def _reicon(self) -> None:
        if self._icon:
            self.setIcon(icon(self._icon, "t4"))

    def set_icon(self, name: str | None) -> None:
        self._icon = name
        self._reicon()


class BottomBar(QWidget):
    """书页底栏（界面稿 .bbar）。"""

    toc_clicked = Signal()
    hl_clicked = Signal()
    prev_clicked = Signal()
    next_clicked = Signal()
    aa_clicked = Signal()

    HEIGHT = 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        g = QGridLayout(self)
        g.setContentsMargins(10, 0, 10, 0)
        g.setSpacing(0)

        left = QHBoxLayout()
        left.setSpacing(4)
        self.toc = BarButton("目录", "toc")
        self.toc.setToolTip("目录：点一项就跳过去")
        self.hl = BarButton("高亮 0", "hl")
        self.hl.setToolTip("这本书里所有的高亮")
        self.note = QLabel("")        # 扫描版的说明（PDF）
        self.note.setFont(theme.font(12))
        self.note.hide()
        left.addWidget(self.toc)
        left.addWidget(self.hl)
        left.addWidget(self.note)
        left.addStretch(1)

        mid = QHBoxLayout()
        mid.setSpacing(2)
        self.prev = BarButton("", "l")
        self.prev.setToolTip("上一页（←）")
        self.next = BarButton("", "r")
        self.next.setToolTip("下一页（→ 或空格）")
        for b in (self.prev, self.next):
            b.setFixedWidth(28)
        self.page = QLabel("")
        self.page.setFont(theme.font(12, tnum=True))
        self.info = QLabel("")
        self.info.setFont(theme.font(12))
        mid.addWidget(self.prev)
        mid.addWidget(self.page)
        mid.addWidget(self.next)
        mid.addSpacing(8)
        mid.addWidget(self.info)
        self.hint = Hint(self, self.info)
        mid.addWidget(self.hint)

        right = QHBoxLayout()
        right.addStretch(1)
        self.aa = BarButton("Aa", None, px=13)
        self.aa.setToolTip("字号、单页 / 双页 / 滚动")
        right.addWidget(self.aa)

        g.addLayout(left, 0, 0)
        g.addLayout(mid, 0, 1)
        g.addLayout(right, 0, 2)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(2, 1)

        self.toc.clicked.connect(self.toc_clicked)
        self.hl.clicked.connect(self.hl_clicked)
        self.prev.clicked.connect(self.prev_clicked)
        self.next.clicked.connect(self.next_clicked)
        self.aa.clicked.connect(self.aa_clicked)

        self._page_args: tuple = (None, None, False, None)
        self._restyle()
        on_theme(self, self._restyle)

    def _restyle(self) -> None:
        self.info.setStyleSheet(theme.qss("color: {t4};"))
        self.note.setStyleSheet(theme.qss("color: {t3};"))
        self.set_page(*self._page_args)

    def set_page(self, cur: int | None, total: int | None, approx: bool = False,
                 last: int | None = None) -> None:
        """last：双页并排时右边那页的页码（底栏写「12–13 / 558」）。"""
        self._page_args = (cur, total, approx, last)
        if cur is None:
            self.page.setText("")
            return
        tot = "…" if total is None else (f"约 {total}" if approx else str(total))
        here = f"{cur}–{last}" if last is not None and last != cur else str(cur)
        self.page.setText(theme.qss(
            '<span style="color:{t2}; font-weight:500">%s</span>'
            '<span style="color:{t3}"> / %s</span>') % (here, tot))

    def set_info(self, text: str) -> None:
        fm = self.info.fontMetrics()
        self.info.setText(fm.elidedText(text, Qt.ElideMiddle, 360))
        self.info.setToolTip(text if len(text) > 24 else "")

    def set_hl_count(self, n: int) -> None:
        self.hl.setText(f"高亮 {n}")

    def set_note(self, text: str | None, tip: str = "", hide_hl: bool = False) -> None:
        """左边的一行说明（扫描版）。hide_hl：这本书根本不能划线时把「高亮 N」藏起来。"""
        self.note.setVisible(bool(text))
        self.note.setText(text or "")
        self.note.setToolTip(tip)
        self.hl.setVisible(not hide_hl)


class Panel(QFrame):
    """从底栏弹出的小面板。书页的子部件（不是独立窗口），点外面、按 Esc 收起。"""

    closed = Signal()

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("panel")
        self.setFocusPolicy(Qt.NoFocus)
        theme.style(self, "QFrame#panel {{ background: {bg3}; border: 1px solid {l2}; border-radius: 10px; }}")
        shadow(self, 30, 12)
        self.hide()
        self._anchor: QWidget | None = None
        self._closed_at = 0.0
        self._closed_by: QWidget | None = None

    def toggle(self, anchor: QWidget, align: str = "left") -> None:
        if self.isVisible():
            self.close_panel()
            return
        # 刚被「点这个按钮」收起的，同一下点击别再打开
        if self._closed_by is anchor and time.monotonic() - self._closed_at < 0.35:
            return
        self.open_at(anchor, align)

    def open_at(self, anchor: QWidget, align: str = "left") -> None:
        self._anchor = anchor
        self.adjustSize()
        host = self.parentWidget()
        a = anchor.mapTo(host, QPoint(0, 0))
        w, h = self.width(), self.height()
        if align == "right":
            x = a.x() + anchor.width() - w
        elif align == "center":
            x = a.x() + (anchor.width() - w) // 2
        else:
            x = a.x()
        x = max(8, min(x, host.width() - w - 8))
        y = max(8, a.y() - h - 8)
        self.move(x, y)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self)

    def close_panel(self, by: QWidget | None = None) -> None:
        if not self.isVisible():
            return
        QApplication.instance().removeEventFilter(self)
        self.hide()
        self._closed_at = time.monotonic()
        self._closed_by = by
        self.closed.emit()

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.MouseButtonPress and self.isVisible():
            gp = ev.globalPosition().toPoint()
            if not self.rect().contains(self.mapFromGlobal(gp)):
                by = None
                if self._anchor is not None and self._anchor.rect().contains(self._anchor.mapFromGlobal(gp)):
                    by = self._anchor
                self.close_panel(by)
        elif t == QEvent.KeyPress and self.isVisible() and ev.key() == Qt.Key_Escape:
            self.close_panel()
            return True
        return False


class ListPanel(Panel):
    """一列可以点的条目（目录、高亮列表）。"""

    picked = Signal(object)

    def __init__(self, host: QWidget, width: int = 340, empty: str = ""):
        super().__init__(host)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(0)
        self.title = QLabel("")
        self.title.setFont(theme.font(12))
        self.title.setContentsMargins(10, 6, 10, 6)
        lay.addWidget(self.title)
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFont(theme.font(13))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setWordWrap(True)
        self.list.setUniformItemSizes(False)
        lay.addWidget(self.list)
        self.empty = QLabel(empty)
        self.empty.setWordWrap(True)
        self.empty.setFont(theme.font(13))
        self.empty.setContentsMargins(10, 8, 10, 12)
        lay.addWidget(self.empty)
        self.setFixedWidth(width)
        theme.style(self.list, """
QListWidget {{ background: transparent; border: 0; outline: 0; color: {t2}; }}
QListWidget::item {{ padding: 6px 10px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {bg4}; color: {t1}; }}
QListWidget::item:selected {{ background: {bg5}; color: {t1}; }}
QListWidget::item:disabled {{ color: {t5}; }}
""")
        self.title.setStyleSheet(theme.qss("color: {t4};"))
        self.empty.setStyleSheet(theme.qss("color: {t3};"))
        on_theme(self, lambda: (self.title.setStyleSheet(theme.qss("color: {t4};")),
                                self.empty.setStyleSheet(theme.qss("color: {t3};"))))
        self.list.itemClicked.connect(self._clicked)

    def set_items(self, title: str, items: list[tuple[str, int, object, bool, bool, str]]) -> None:
        """items：(文字, 缩进层级, 数据, 能不能点, 是不是当前, 提示)。"""
        self.title.setText(title)
        self.list.clear()
        cur_row = -1
        for i, (text, level, data, enabled, current, tip) in enumerate(items):
            it = QListWidgetItem(("　" * max(0, level - 1)) + text)
            it.setData(Qt.UserRole, data)
            if not enabled:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)
            if tip:
                it.setToolTip(tip)
            if current:
                f = theme.font(13, 600)
                it.setFont(f)
                cur_row = i
            self.list.addItem(it)
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))
        host = self.parentWidget()
        rows = sum(self.list.sizeHintForRow(r) for r in range(min(self.list.count(), 60))) + 8
        self.list.setFixedHeight(max(40, min(rows, int(host.height() * 0.6) if host else 420)))
        if cur_row >= 0:
            self.list.setCurrentRow(cur_row)
            self.list.scrollToItem(self.list.item(cur_row), QListWidget.PositionAtCenter)

    def _clicked(self, it: QListWidgetItem) -> None:
        if not (it.flags() & Qt.ItemIsEnabled):
            return
        self.close_panel()
        self.picked.emit(it.data(Qt.UserRole))


_SEG = """
QPushButton {{ background: transparent; border: 0; color: {t3}; font-size: 12px; height: 24px;
  padding: 0 10px; border-radius: 5px; }}
QPushButton:hover {{ color: {t1}; }}
QPushButton:checked {{ background: {bg2}; color: {t1}; font-weight: 600; }}
QPushButton:disabled {{ color: {t5}; }}
"""


class Seg(QFrame):
    """分段选择（界面稿 .seg）：一次只能选一个。"""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str, parent=None):
        super().__init__(parent)
        self.setObjectName("seg")
        theme.style(self, "QFrame#seg {{ background: {bg4}; border-radius: 7px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in options:
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.setFont(theme.font(12))
            theme.style(b, _SEG)
            b.clicked.connect(lambda _=False, k=key: self.set_current(k, emit=True))
            lay.addWidget(b)
            self.buttons[key] = b
        self.set_current(current)

    def set_current(self, key: str, emit: bool = False) -> None:
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        if emit:
            self.changed.emit(key)


class AaPanel(Panel):
    """Aa 面板：一行一个设置（左边名字，右边控件）。"""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(14, 12, 14, 12)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(10)
        self._labels: list[QLabel] = []
        on_theme(self, self._restyle)

    def add_row(self, name: str, widget: QWidget) -> None:
        r = self.grid.rowCount()
        lab = QLabel(name)
        lab.setFont(theme.font(12))
        lab.setStyleSheet(theme.qss("color: {t4};"))
        self._labels.append(lab)
        self.grid.addWidget(lab, r, 0)
        self.grid.addWidget(widget, r, 1, Qt.AlignLeft)

    def _restyle(self) -> None:
        for lab in self._labels:
            lab.setStyleSheet(theme.qss("color: {t4};"))


class Stepper(QFrame):
    """A− 18 A+：有界的数值（字号）。"""

    changed = Signal(int)

    def __init__(self, values: list[int], current: int, parent=None):
        super().__init__(parent)
        self.values = values
        self.setObjectName("step")
        theme.style(self, "QFrame#step {{ background: {bg4}; border-radius: 7px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.minus = QPushButton("A−")
        self.plus = QPushButton("A+")
        self.val = QLabel("")
        self.val.setAlignment(Qt.AlignCenter)
        self.val.setFixedWidth(40)
        self.val.setFont(theme.font(12, tnum=True))
        for b in (self.minus, self.plus):
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            theme.style(b, _SEG)
        self.minus.setToolTip("字小一点")
        self.plus.setToolTip("字大一点")
        lay.addWidget(self.minus)
        lay.addWidget(self.val)
        lay.addWidget(self.plus)
        self.minus.clicked.connect(lambda: self._step(-1))
        self.plus.clicked.connect(lambda: self._step(1))
        self.cur = current
        self._show()

    def _show(self) -> None:
        self.val.setText(f"{self.cur}px")
        self.val.setStyleSheet(theme.qss("color: {t1};"))
        i = self.values.index(self.cur) if self.cur in self.values else 0
        self.minus.setEnabled(i > 0)
        self.plus.setEnabled(i < len(self.values) - 1)

    def _step(self, d: int) -> None:
        i = self.values.index(self.cur) if self.cur in self.values else 0
        i = max(0, min(len(self.values) - 1, i + d))
        if self.values[i] != self.cur:
            self.cur = self.values[i]
            self._show()
            self.changed.emit(self.cur)


class BackPill(QFrame):
    """「跳到了原文 · 第 N 页　[回到刚才的第 M 页]」。"""

    back_clicked = Signal()

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("pill")
        theme.style(self, "QFrame#pill {{ background: {bg3}; border: 1px solid {l2}; border-radius: 15px; }}")
        shadow(self, 24, 8)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 2, 3, 2)
        lay.setSpacing(10)
        self.text = QLabel("")
        self.text.setFont(theme.font(12))
        self.btn = QPushButton("")
        self.btn.setFocusPolicy(Qt.NoFocus)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.setFont(theme.font(12))
        self.btn.setIconSize(QSize(16, 16))
        theme.style(self.btn, """
QPushButton {{ height: 24px; padding: 0 10px; border-radius: 12px; background: {bg4};
  border: 1px solid {l2}; color: {t1}; font-size: 12px; }}
QPushButton:hover {{ background: {bg5}; }}""")
        lay.addWidget(self.text)
        lay.addWidget(self.btn)
        self.btn.clicked.connect(self.back_clicked)
        self._restyle()
        on_theme(self, self._restyle)
        self.hide()

    def _restyle(self) -> None:
        self.text.setStyleSheet(theme.qss("color: {t2};"))
        self.btn.setIcon(icon("back", "t2"))

    def show_jump(self, here: str, back: str | None) -> None:
        self.text.setText(f"跳到了原文 · {here}")
        self.btn.setVisible(back is not None)
        if back is not None:
            self.btn.setText(f"回到刚才的{back}")
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()

    def place(self) -> None:
        host = self.parentWidget()
        self.adjustSize()
        # 贴着书页顶边（界面稿 top:14；这里再往上挪一点，尽量不盖住第一行字）
        self.move((host.width() - self.width()) // 2, 5)


class Hint(QFrame):
    """底栏中间留着的一行字（挡住下一节时：「先答完左边这道题」）。

    放在底栏里、顶替「所在章 · 百分比」，不盖住书页上的字。不会自己消失：往回翻、挡板换了才走。
    """

    def __init__(self, host: QWidget, partner: QWidget | None = None):
        super().__init__(host)
        self.partner = partner       # 显示时藏起来的那个（底栏的章名 · 百分比）
        self.setObjectName("hint")
        theme.style(self, "QFrame#hint {{ background: {bg3}; border: 1px solid {l2}; border-radius: 11px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 2, 11, 2)
        lay.setSpacing(6)
        self.dot = QLabel()
        self.dot.setFixedSize(7, 7)
        self.text = QLabel("")
        self.text.setFont(theme.font(12))
        lay.addWidget(self.dot)
        lay.addWidget(self.text)
        self._restyle()
        on_theme(self, self._restyle)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        super().hide()

    def _restyle(self) -> None:
        self.text.setStyleSheet(theme.qss("color: {t1};"))
        self.dot.setStyleSheet(theme.qss("background: {acc}; border-radius: 3px;"))

    def show_text(self, text: str) -> None:
        self.text.setText(text)
        self.setToolTip("这一节读完了，左边有一道小测。答完就能接着往后读；往前翻不受影响。")
        if self.partner is not None:
            self.partner.hide()
        self.show()

    def hide(self) -> None:
        super().hide()
        if self.partner is not None:
            self.partner.show()

    def place(self) -> None:
        pass


class DimOverlay(QWidget):
    """发呆 / 离开时蒙在书页上的一层（界面稿：纸色 62%）。不拦鼠标。"""

    def __init__(self, host: QWidget, key: str):
        super().__init__(host)
        self.key = key
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.color(self.key, 0.62))


class _Stage(QWidget):
    """放书页画布和各种小条的地方；尺寸变了就把小条摆回原位。"""

    resized = Signal()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.resized.emit()


# ── BookView ─────────────────────────────────────────────────
class BookView(QWidget):
    """书页的统一接口（UI_PLAN「B」）。EpubView / PdfView 实现具体的排版和绘制。"""

    # 信号
    activity = Signal()                  # 翻页、滚动、点击、选字：用来防发呆
    location_changed = Signal(object)    # Loc：当前看到的位置
    ask_ai = Signal(str, object)         # 选中的文字、它的 Loc
    highlights_changed = Signal()        # 高亮加了 / 删了
    barrier_hit = Signal(int)            # 读过了（挡板模式：想翻过）这一节的末尾（小节号）

    BG_KEY = "paper"          # 书页底色
    DIM_KEY = "paper"         # 蒙层颜色
    # 小测挡不挡着往后读。2026-09-25 起：不挡（想跳过导读直接读第一章，被一直拦着）。
    # 不挡时「挡板」只是个记号：一页一页读过这一节的末尾，发一次 barrier_hit（左栏出题），照样往后翻；
    # 目录、跳转随便跳，跳过去的节不出题。改回 True 就是原来的「答完才能往后读」。
    BLOCK = False
    BARRIER_TEXT = "先答完左边这道题"
    BARRIER_REPEAT = 0.6      # 连续滚轮撞墙时，barrier_hit 最多每 0.6 秒发一次

    def __init__(self, parent=None):
        super().__init__(parent)
        self.book: Book | None = None
        self._barrier: int | None = None
        self._barrier_last = 0.0
        self._barrier_passed: int | None = None   # 不挡时：这个挡板已经发过 barrier_hit 了
        self._continuous = False
        self._hls: list[dict] = []
        self._last_loc: Loc | None = None
        self._return_to = None
        self._src_label: str | None = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

        self.stage = _Stage(self)
        self.bar = BottomBar(self)
        # 外面的样式表（比如「QWidget { background: … }」）不要给书页里的字刷上底色
        self.setStyleSheet("QLabel { background: transparent; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.stage, 1)
        lay.addWidget(self.bar)

        self.pill = BackPill(self.stage)
        self.hint = self.bar.hint
        self.dim = DimOverlay(self.stage, self.DIM_KEY)
        from .popover import SelectionPopover
        self.popover = SelectionPopover(self.stage)
        self.toc_panel = ListPanel(self, 340)
        self.hl_panel = ListPanel(self, 360, "还没有高亮。选中一段字，点小条上的「高亮」就行。")
        self.aa_panel: AaPanel | None = None

        self.stage.resized.connect(self._place_overlays)
        self.pill.back_clicked.connect(self._go_back)
        self.bar.toc_clicked.connect(self._open_toc)
        self.bar.hl_clicked.connect(self._open_hls)
        self.bar.prev_clicked.connect(lambda: self._user_turn(-1))
        self.bar.next_clicked.connect(lambda: self._user_turn(1))
        self.bar.aa_clicked.connect(self._open_aa)
        self.toc_panel.picked.connect(self._toc_picked)
        self.hl_panel.picked.connect(self._hl_picked)
        self.popover.ask.connect(self._pop_ask)
        self.popover.highlight.connect(self._pop_highlight)
        self.popover.unhighlight.connect(self._pop_unhighlight)
        self._watch(self)
        on_theme(self, self._theme_changed)

    # ── 接口（子类实现带 * 的） ─────────────────────────────
    def open(self, book: Book, loc: Loc | None, highlights: list[dict]) -> None:   # *
        raise NotImplementedError

    def goto(self, loc: Loc) -> None:   # *
        raise NotImplementedError

    def current_loc(self) -> Loc:   # *
        raise NotImplementedError

    def page_label(self, loc: Loc) -> str:   # *
        raise NotImplementedError

    def mark_source(self, loc: Loc, quote: str) -> None:   # *
        raise NotImplementedError

    def source_label(self) -> str | None:
        """最近一次 mark_source 跳到的那页（「第 134 页」，跟顶上小条写的一样）；没跳过 / 已清掉是 None。"""
        return self._src_label

    def clear_source(self) -> None:
        self.pill.hide()
        self._src_label = None
        self._return_to = None
        self._clear_source_mark()
        self.update_view()

    def highlights(self) -> list[dict]:
        return [dict(h) for h in self._hls]

    def set_barrier(self, section_idx: int | None) -> None:
        self._barrier = section_idx
        self._barrier_passed = None
        self.hint.hide()
        self.update_view()

    def barrier(self) -> int | None:
        return self._barrier

    def set_dimmed(self, on: bool) -> None:
        self.dim.setVisible(on)
        if on:
            self.dim.setGeometry(self.stage.rect())
            self.dim.raise_()
            self.popover.hide()

    # 额外的公开方法（读书界面可以直接调，比如把 ←→ 转过来）
    def page_forward(self) -> None:   # *
        raise NotImplementedError

    def page_back(self) -> None:   # *
        raise NotImplementedError

    # ── 子类要实现的小钩子 ───────────────────────────────
    def update_view(self) -> None:
        """重画书页 + 刷新底栏。"""

    def _clear_source_mark(self) -> None:
        pass

    def _scroll_lines(self, n: int) -> None:
        pass

    def _hl_items(self) -> list[tuple[str, int, object, bool, bool, str]]:
        return []

    def _goto_highlight(self, h: dict) -> None:
        pass

    def _toc_allowed(self, loc: Loc) -> bool:
        return not self.BLOCK or self._barrier_loc() is None or loc < self._barrier_loc()

    def _build_aa(self, panel: AaPanel) -> None:
        pass

    def _go_back(self) -> None:
        pass

    def _pop_ask(self) -> None:
        pass

    def _pop_highlight(self) -> None:
        pass

    def _pop_unhighlight(self) -> None:
        pass

    def _theme_changed(self) -> None:
        self.update()
        self.update_view()

    # ── 公共逻辑 ─────────────────────────────────────────
    def _barrier_loc(self) -> Loc | None:
        """被挡住的位置（这一节的终点，不含）。到书尾就等于不挡。"""
        if self._barrier is None or self.book is None:
            return None
        secs = self.book.sections
        if not 0 <= self._barrier < len(secs):
            return None
        end = secs[self._barrier].end
        return None if end >= self.book.end() else end

    def _blocked(self) -> bool:
        """往后翻（滚）过了挡板。返回 True = 挡住，调用的地方停下。

        挡（BLOCK）：书页底部写一行，发 barrier_hit。
        不挡：这个挡板第一次读过时发一次 barrier_hit（左栏出题），照样往后翻。
        """
        if not self.BLOCK:
            if self._barrier_passed != self._barrier:
                self._barrier_passed = self._barrier
                self.barrier_hit.emit(self._barrier)
            return False
        self.hint.show_text(self.BARRIER_TEXT)
        now = time.monotonic()
        # 按键、点按钮：每次都发；滚轮、拖滚动条是连着来的：最多每 0.6 秒发一次
        if not self._continuous or now - self._barrier_last >= self.BARRIER_REPEAT:
            self._barrier_last = now
            self.barrier_hit.emit(self._barrier)
        return True

    def _continuous_input(self, fn, *args) -> None:
        """滚轮、滚动条这类连着来的输入：撞挡板时不要一下发几十次 barrier_hit。"""
        self._continuous = True
        try:
            fn(*args)
        finally:
            self._continuous = False

    def _act(self) -> None:
        self.activity.emit()

    def _emit_location(self) -> None:
        loc = self.current_loc()
        if loc != self._last_loc:
            self._last_loc = loc
            self.location_changed.emit(loc)

    def _user_turn(self, d: int) -> None:
        self._act()
        if d > 0:
            self.page_forward()
        else:
            self.page_back()

    def _watch(self, w: QWidget) -> None:
        """点击、滚轮都算读书活动（子部件也算）。"""
        w.installEventFilter(self)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() in (QEvent.MouseButtonPress, QEvent.Wheel):
            self._act()
        return False

    def showEvent(self, e) -> None:
        super().showEvent(e)
        for w in self.findChildren(QWidget):   # 子部件（画布、底栏按钮、小条）上的点击也算
            w.installEventFilter(self)

    def _place_overlays(self) -> None:
        r = self.stage.rect()
        self.dim.setGeometry(r)
        if self.pill.isVisible():
            self.pill.place()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.color(self.BG_KEY))

    def keyPressEvent(self, e: QKeyEvent) -> None:
        k = e.key()
        shift = bool(e.modifiers() & Qt.ShiftModifier)
        if k in (Qt.Key_Right, Qt.Key_PageDown) or (k == Qt.Key_Space and not shift):
            self._act()
            self.page_forward()        # 翻一屏（PDF 连续滚动时是往下一屏，不跳过半页）
        elif k in (Qt.Key_Left, Qt.Key_PageUp) or (k == Qt.Key_Space and shift):
            self._act()
            self.page_back()
        elif k == Qt.Key_Down:
            self._act()
            self._scroll_lines(1)
        elif k == Qt.Key_Up:
            self._act()
            self._scroll_lines(-1)
        elif k == Qt.Key_Escape and self.popover.isVisible():
            self.popover.dismiss()
        else:
            super().keyPressEvent(e)

    # 底栏的面板
    def _open_toc(self) -> None:
        if self.book is None:
            return
        cur = self.current_loc()
        toc = self.book.toc
        items = []
        cur_i, best = -1, None
        for i, e in enumerate(toc):      # 跟底栏、顶条写的章名同一个算法（Book.place_title）
            if e.loc <= cur and (best is None or e.loc >= best):
                cur_i, best = i, e.loc
        for i, e in enumerate(toc):
            ok = self._toc_allowed(e.loc) or e.loc <= cur
            items.append((e.title, e.level, e.loc, ok, i == cur_i,
                          "" if ok else "答完左边这道题，才能往后看"))
        if not items:   # 没有目录：用小节
            for s in self.book.sections:
                ok = self._toc_allowed(s.start) or s.start <= cur
                items.append((s.title, 1, s.start, ok, s.contains(cur),
                              "" if ok else "答完左边这道题，才能往后看"))
        self.toc_panel.set_items("目录", items)
        self.toc_panel.toggle(self.bar.toc, "left")

    def _toc_picked(self, loc: Loc) -> None:
        self._act()
        if not (self._toc_allowed(loc) or loc <= self.current_loc()) and self._blocked():
            return
        self.goto(loc)

    def _open_hls(self) -> None:
        items = self._hl_items()
        self.hl_panel.set_items(f"高亮 · {len(items)} 处" if items else "高亮", items)
        self.hl_panel.toggle(self.bar.hl, "left")

    def _hl_picked(self, h: dict) -> None:
        self._act()
        self._goto_highlight(h)

    def _open_aa(self) -> None:
        if self.aa_panel is not None and self.aa_panel.isVisible():
            self.aa_panel.close_panel()
            return
        if self.aa_panel is not None:
            self.aa_panel.deleteLater()
        self.aa_panel = AaPanel(self)
        self._build_aa(self.aa_panel)
        self.aa_panel.toggle(self.bar.aa, "right")

    def _chapter_title(self, loc: Loc) -> str:
        """底栏中间：所在章（目录里最近的一项；没有目录用小节名）。"""
        return self.book.place_title(loc) if self.book is not None else ""


# ── 演示用的外框（各模块的 __main__ 共用，不进锁屏） ───────────────────
def demo_window(view: BookView, title: str) -> QWidget:
    """1280×800 的演示窗：左边 340px 是 AI 栏的占位，顶上 40px 是顶条的占位，右边是书页。

    按键：F2 换主题　F3 挡住当前这一节　F4 在当前这一节里标一句出处　F5 蒙层开关。
    """
    win = QWidget()
    win.setWindowTitle(title)
    win.resize(1280, 800)
    outer = QVBoxLayout(win)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    strip = QLabel("  （顶条：读书界面负责）　F2 换主题 · F3 挡住这一节 · F4 标一句出处 · F5 蒙层")
    strip.setFixedHeight(40)
    strip.setFont(theme.font(12))
    theme.style(strip, "QLabel {{ background: {bg1}; color: {t4}; border-bottom: 1px solid {l1}; }}")
    mid = QHBoxLayout()
    mid.setSpacing(0)
    left = QLabel("AI 栏\n（C 负责，这里是占位）")
    left.setAlignment(Qt.AlignCenter)
    left.setFixedWidth(340)
    left.setFont(theme.font(13))
    theme.style(left, "QLabel {{ background: {bg2}; color: {t5}; border-right: 1px solid {l1}; }}")
    mid.addWidget(left)
    mid.addWidget(view, 1)
    outer.addWidget(strip)
    outer.addLayout(mid, 1)
    win.setObjectName("demo")
    theme.style(win, "QWidget#demo {{ background: {bg2}; }}")

    log = QLabel("", view)   # 信号的回显（演示用）

    def _log(s: str) -> None:
        print("[书页]", s)

    view.barrier_hit.connect(lambda i: _log(f"barrier_hit({i})"))
    view.ask_ai.connect(lambda t, loc: _log(f"ask_ai({t[:30]!r}…, {loc})"))
    view.highlights_changed.connect(lambda: _log(f"highlights_changed：{len(view.highlights())} 条"))
    view.location_changed.connect(lambda loc: _log(f"location_changed({loc}) {view.page_label(loc)}"))
    log.hide()

    class _Keys(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_F2, Qt.Key_F3, Qt.Key_F4, Qt.Key_F5):
                k = ev.key()
                if k == Qt.Key_F2:
                    theme.set_theme("light" if theme.current() == "dark" else "dark", save=False)
                elif k == Qt.Key_F3 and view.book is not None:
                    s = view.book.section_at(view.current_loc())
                    view.set_barrier(None if view.barrier() is not None else (s.idx if s else None))
                    _log(f"挡板：{view.barrier()}")
                elif k == Qt.Key_F4 and view.book is not None:
                    _demo_source(view)
                elif k == Qt.Key_F5:
                    view.set_dimmed(not view.dim.isVisible())
                return True
            return False

    keys = _Keys(win)
    QApplication.instance().installEventFilter(keys)
    win._keys = keys   # 留住
    return win


def _demo_source(view: BookView) -> None:
    """在当前这一节里挑一句当「出处」，演示跳转 + 陶土色标记 + 回跳小条。"""
    book = view.book
    s = book.section_at(view.current_loc())
    if s is None:
        return
    text = s.text()
    import re
    sents = [x for x in re.split(r"(?<=[。！？.!?])", text) if 20 <= len(x.strip()) <= 80]
    if not sents:
        return
    q = sents[len(sents) // 2].strip()
    from reader.ai.companion import locate_quote
    loc = locate_quote(book, s, q) or s.start
    view.mark_source(loc, q)


def demo_main(kind: str, default_glob: str, argv: list[str], after=None) -> int:
    """各模块 __main__ 共用：python -m reader.ui.bookview.epub_view [书的路径]。"""
    import glob
    import os
    import sys

    theme.prepare_platform()
    app = QApplication(sys.argv[:1])
    fam = theme.load_fonts()
    theme.init_from_saved()
    theme.apply_app(app)
    print(f"字体：{fam}　引擎：{theme.engine_name()}")
    path = argv[0] if argv else ""
    if not path:
        hits = sorted(glob.glob(os.path.expanduser(default_glob)))
        if not hits:
            print(f"~/Downloads 里没找到 {default_glob}，请把书的路径写在命令后面")
            return 1
        path = hits[0]
    from reader.books import open_book
    t0 = time.perf_counter()
    book = open_book(path)
    t1 = time.perf_counter()
    from . import create_view
    view = create_view(book)
    win = demo_window(view, f"书页演示 · {book.title}")
    win.show()
    t2 = time.perf_counter()
    view.open(book, None, [])
    t3 = time.perf_counter()
    print(f"《{book.title}》拆书 {1000 * (t1 - t0):.0f}ms，建书页 {1000 * (t2 - t1):.0f}ms，"
          f"open() {1000 * (t3 - t2):.0f}ms")
    view.setFocus()
    if after is not None:
        QTimer.singleShot(600, lambda: after(view))
    return app.exec()


if __name__ == "__main__":
    # 演示：python -m reader.ui.bookview.base [书的路径]　—— 按书的类型开 EPUB / PDF 书页（不进锁屏）
    import sys
    sys.exit(demo_main("", "~/Downloads/*.epub", sys.argv[1:]))
