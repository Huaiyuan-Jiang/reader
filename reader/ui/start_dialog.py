"""「开始读」窗口（界面稿板 2）。

- 默认时长 = 今天还差的分钟数；今天已经读满就默认 15 分钟，标题下写「今天读满了，这次是加读」。
- 常用的做成按钮（15 / 补满今天 / 45），旁边能自己填；能填 5–180，超过 90 在框下面提醒一句（不弹框）。
- 选了之后下面一句跟着变：「读完这次，今天正好满 45 分钟」/「读完今天 33 分钟，还差 12」。
- 开始之后会怎样，按按钮之前全写出来（知识放在眼前）。主按钮写清时长「开始读 27 分钟」。
- Esc = 取消。

盖在书架窗口上：一层半透明遮罩 + 中间一张卡（无边框的模态窗口，跟书架窗口一样大）。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QIntValidator, QPainter
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from reader.books import Loc
from reader.ui import theme
from reader.ui.shelf_kit import Button, Card, Cover, IconLabel, Label, cap, hline, lbl, on_theme

MIN_MINUTES = 5
MAX_MINUTES = 180
LONG_MINUTES = 90
EXTRA_DEFAULT = 15


def default_minutes(today: int, target: int = 45) -> int:
    """今天还差多少就默认读多少（至少 5）；已经读满了默认 15（加读）。"""
    missing = target - today
    if missing <= 0:
        return EXTRA_DEFAULT
    return max(MIN_MINUTES, min(MAX_MINUTES, missing))


def presets(today: int, target: int = 45) -> list[tuple[int, str]]:
    """按钮：[(分钟, 按钮上的字)]。"""
    missing = target - today
    if missing <= 0:
        return [(15, "15 分钟"), (30, "30 分钟"), (45, "45 分钟")]
    fill = default_minutes(today, target)
    items = sorted({15, fill, 45})
    return [(m, f"{m} 分钟 · 补满今天" if m == fill else f"{m} 分钟") for m in items]


def outcome(minutes: int, today: int, target: int = 45) -> str:
    """选了时长后下面那一句。"""
    after = today + minutes
    if today >= target:
        return f"今天已经读满了，这 {minutes} 分钟算加读，读完今天一共 {after} 分钟。"
    if after == target:
        return f"读完这次，今天正好满 {target} 分钟。"
    if after < target:
        return f"读完今天 {after} 分钟，还差 {target - after}。"
    return f"读完这次，今天满 {target} 分钟，还多读 {after - target} 分钟。"


def check_minutes(text: str) -> tuple[int | None, str]:
    """自己填的分钟数：(能用的分钟数或 None, 框下面的提醒)。"""
    t = text.strip()
    if not t:
        return None, ""
    try:
        m = int(t)
    except ValueError:
        return None, f"填 {MIN_MINUTES} 到 {MAX_MINUTES} 之间的整数分钟。"
    if m < MIN_MINUTES or m > MAX_MINUTES:
        return None, f"一次能读 {MIN_MINUTES} 到 {MAX_MINUTES} 分钟，{m} 分钟不行。"
    if m > LONG_MINUTES:
        return m, f"{m} 分钟是很长的一次：开始后要读满才放你走，中途只能走紧急出口。"
    return m, ""


def resume_text(entry, place: dict) -> str:
    """「从第 132 页「3.2 某一节」接着读」"""
    sec = place.get("section") or ""
    pos: Loc | None = entry.position
    if pos is None:
        return "从头开始读"
    quoted = f"「{sec}」" if sec else ""
    if entry.kind == "pdf":
        return f"从第 {pos.doc + 1} 页{quoted}接着读"
    return f"从{quoted}接着读" if sec else "从上次读到的地方接着读"


def scan_line(entry, ocr_state: tuple | None) -> str:
    """扫描版那一句（没识别 / 识别中 / 识别完）。不是扫描版返回 ""。"""
    if not entry.scanned:
        return ""
    kind, done, total = ocr_state or ("unavailable", 0, entry.pages)
    if kind == "done":
        return "扫描版 · 已识别文字"
    if kind == "unavailable":
        return "扫描版：不能划线，AI 看不到正文"
    return f"扫描版 · 正在识别文字 {done} / {total} 页"


class _PresetButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(theme.font(13, 400, tnum=True))
        self._restyle()
        on_theme(self, self._restyle)
        self.toggled.connect(lambda on: self.setFont(theme.font(13, 600 if on else 400, tnum=True)))

    def _restyle(self) -> None:
        t = theme.THEMES[theme.current()]
        self.setStyleSheet(
            f"QPushButton {{ height: 34px; padding: 0 14px; border-radius: 8px; font-size: 13px; }}"
            f"QPushButton:checked {{ border-color: {t['focus']}; background: {t['bg5']}; }}"
            f"QPushButton:focus {{ border-color: {t['focus']}; }}")


class _CustomField(QFrame):
    """「自己填 [  ] 分钟」：长得像按钮的输入框。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("custom")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.setSpacing(4)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("自己填")
        self.edit.setValidator(QIntValidator(0, 999, self))
        self.edit.setFont(theme.font(13, 400, tnum=True))
        self.edit.setFixedWidth(46)
        self.edit.setAccessibleName("自己填分钟数")
        self.unit = Label("分钟", 13, 400, "t4")
        lay.addWidget(self.edit)
        lay.addWidget(self.unit)
        self.setFixedHeight(34)
        self.active = False
        self._restyle()
        on_theme(self, self._restyle)

    def set_active(self, on: bool) -> None:
        self.active = on
        self._restyle()

    def _restyle(self) -> None:
        t = theme.THEMES[theme.current()]
        border = t["focus"] if self.active else t["l2"]
        self.setStyleSheet(
            f"#custom {{ background: {t['bg0']}; border: 1px solid {border}; border-radius: 8px; }}"
            f"#custom QLineEdit {{ background: transparent; border: 0; color: {t['t1']}; "
            f"selection-background-color: {t['bg5']}; font-size: 13px; }}")


class StartDialog(QDialog):
    """用法：dlg = StartDialog(window, entry, library, today_minutes=18, ai_ready=True)
    if dlg.exec(): minutes = dlg.minutes()"""

    def __init__(self, parent: QWidget, entry, library=None, *, today_minutes: int = 0,
                 target: int = 45, ai_ready: bool | None = True, ocr_state: tuple | None = None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self.setWindowTitle("开始读")
        self.entry = entry
        self.today = int(today_minutes)
        self.target = int(target)
        self._minutes: int | None = default_minutes(self.today, self.target)
        place = library.place(entry.id) if library is not None else {}

        self.card = Card("bg3", "l2", 14, shadow=(30, 90))
        self.card.setParent(self)
        self.card.setFixedWidth(500)
        v = QVBoxLayout(self.card)
        v.setContentsMargins(26, 24, 26, 24)
        v.setSpacing(0)

        v.addWidget(Label("开始读", 19, 600, "t1"))
        if self.today >= self.target:
            v.addSpacing(4)
            v.addWidget(Label("今天读满了，这次是加读。", 13, 400, "greent"))

        # 这本书
        book = Card("bg2", "l1", 10)
        bl = QHBoxLayout(book)
        bl.setContentsMargins(12, 12, 12, 12)
        bl.setSpacing(14)
        bl.addWidget(Cover(entry.title, entry.cover, 34, 48, shadow=False))
        info = QVBoxLayout()
        info.setSpacing(2)
        info.addStretch(1)
        title = Label(f"《{entry.title}》", 14, 400, "t1")
        title.setToolTip(entry.path)
        info.addWidget(title)
        info.addWidget(cap(resume_text(entry, place)))
        scan = scan_line(entry, ocr_state)
        if scan:
            info.addWidget(cap(scan, "t3"))
        info.addStretch(1)
        bl.addLayout(info, 1)
        v.addSpacing(16)
        v.addWidget(book)
        v.addSpacing(20)

        # 这次读多久
        v.addWidget(lbl("这次读多久"))
        v.addSpacing(8)
        hl = QHBoxLayout()
        hl.setSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.preset_buttons: dict[int, _PresetButton] = {}
        for m, text in presets(self.today, self.target):
            b = _PresetButton(text)
            self.group.addButton(b, m)
            self.preset_buttons[m] = b
            hl.addWidget(b)
        self.custom = _CustomField()
        hl.addWidget(self.custom)
        hl.addStretch(1)
        v.addLayout(hl)
        self.group.idClicked.connect(self._pick_preset)
        self.custom.edit.textEdited.connect(self._custom_edited)
        self.custom.edit.returnPressed.connect(self._try_accept)
        self.custom.edit.installEventFilter(self)

        v.addSpacing(8)
        self.warn = Label("", 12, 400, "acc", wrap=True)      # 超 90 / 越界：框下面一句，不弹框
        self.warn.setVisible(False)
        v.addWidget(self.warn)
        self.outcome = Label("", 12, 400, "t3", tnum=True, wrap=True)
        v.addWidget(self.outcome)

        v.addSpacing(20)
        v.addWidget(hline("l1"))
        v.addSpacing(16)

        # 开始后
        v.addWidget(lbl("开始后"))
        v.addSpacing(8)
        if ai_ready is False:
            quiz_line = "AI 没登录：这次左边问不了 AI，也没有小节小测（书照样能读）"
        elif entry.scanned and (ocr_state is None or ocr_state[0] == "unavailable"):
            quiz_line = "这本是扫描版：AI 看不到正文，不出小测"
        elif entry.scanned and ocr_state[0] != "done":
            quiz_line = "每读完一小节，左边 AI 问你 1 道题（还没识别出文字的页不出题）"
        else:
            quiz_line = "每读完一小节，左边 AI 问你 1 道题"
        for text in ("阅读窗口全屏、盖在最上面；副屏盖上深色遮罩",
                     "别的软件在底下照常跑；切出去计时就停，记一次「离开」",
                     "5 分钟没动静，计时先停",
                     quiz_line,
                     "真有急事：右上角「紧急出口」，要确认几次，会记下来"):
            line = QHBoxLayout()
            line.setSpacing(8)
            dot = Label("•", 13, 400, "t2")
            dot.setFixedWidth(10)
            line.addWidget(dot, 0, Qt.AlignTop)
            t = Label(text, 13, 400, "t2", wrap=True)
            t.setMinimumHeight(24)
            line.addWidget(t, 1)
            v.addLayout(line)

        tip = Card("bg4", None, 8)
        tl = QHBoxLayout(tip)
        tl.setContentsMargins(12, 8, 12, 8)
        tl.setSpacing(8)
        tl.addWidget(IconLabel("file", "t2", 16))
        tl.addWidget(Label("下载、渲染这些可以放着，在后台照常跑。", 13, 400, "t2"), 1)
        v.addSpacing(14)
        v.addWidget(tip)

        # 按钮
        v.addSpacing(22)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addStretch(1)
        self.cancel_btn = Button("取消", "ghost")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        self.start_btn = Button("开始读", "pri")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._try_accept)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.start_btn)
        v.addLayout(foot)

        if self._minutes in self.preset_buttons:
            self.preset_buttons[self._minutes].setChecked(True)
        else:
            self.custom.edit.setText(str(self._minutes))
            self.custom.set_active(True)
        self._update()
        repaint = lambda: self.update()  # noqa: E731
        on_theme(self, repaint)

    # ── 对外 ────────────────────────────────────────────
    def minutes(self) -> int | None:
        return self._minutes

    # ── 选时长 ──────────────────────────────────────────
    def _pick_preset(self, m: int) -> None:
        self._minutes = m
        self.custom.edit.clear()
        self.custom.set_active(False)
        self._update()

    def _custom_edited(self, text: str) -> None:
        m, _ = check_minutes(text)
        self.custom.set_active(bool(text.strip()))
        if text.strip():
            self.group.setExclusive(False)
            for b in self.preset_buttons.values():
                b.setChecked(False)
            self.group.setExclusive(True)
            self._minutes = m
        else:                                   # 清空了：回到默认那个按钮
            d = default_minutes(self.today, self.target)
            self._minutes = d
            if d in self.preset_buttons:
                self.preset_buttons[d].setChecked(True)
        self._update()

    def _update(self) -> None:
        text = self.custom.edit.text()
        _, warn = check_minutes(text) if text.strip() else (None, "")
        self.warn.setText(warn)
        self.warn.setVisible(bool(warn))
        bad = self._minutes is None
        self.warn.set_color("redt" if bad else "acc")
        if bad:
            self.outcome.setVisible(False)
            self.start_btn.setText("开始读")
            self.start_btn.setEnabled(False)
        else:
            self.outcome.setText(outcome(self._minutes, self.today, self.target))
            self.outcome.setVisible(True)
            self.start_btn.setText(f"开始读 {self._minutes} 分钟")
            self.start_btn.setEnabled(True)
        self.card.adjustSize()
        self._place_card()

    def _try_accept(self) -> None:
        if self._minutes is not None and self.start_btn.isEnabled():
            self.accept()

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.custom.edit and ev.type() == ev.Type.FocusIn:
            self.custom.set_active(True)
        elif obj is self.custom.edit and ev.type() == ev.Type.FocusOut and not self.custom.edit.text().strip():
            self.custom.set_active(False)
        return super().eventFilter(obj, ev)

    # ── 盖在书架上 ──────────────────────────────────────
    def showEvent(self, e) -> None:
        self._cover_parent()
        super().showEvent(e)
        if self.preset_buttons:
            btn = self.group.checkedButton()
            (btn or self.start_btn).setFocus()

    def _cover_parent(self) -> None:
        p = self.parentWidget()
        if p is None:
            self.resize(640, 800)
        else:
            win = p.window()
            tl = win.mapToGlobal(QPoint(0, 0))
            self.setGeometry(QRect(tl, win.size()))
        self._place_card()

    def _place_card(self) -> None:
        self.card.adjustSize()
        cw, ch = self.card.width(), self.card.sizeHint().height()
        self.card.resize(cw, ch)
        self.card.move((self.width() - cw) // 2, max(16, (self.height() - ch) // 2))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._place_card()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.color("scrim"))
        p.end()


if __name__ == "__main__":
    # 演示：用 ~/Downloads 里的一本真书弹出「开始读」。加 --today 45 看「加读」，--no-ai 看没登录。
    # 只打印选了多少分钟，不会开始读、不会锁屏。
    import sys
    import tempfile
    from pathlib import Path

    from PySide6.QtWidgets import QApplication, QMainWindow

    from reader.library import Library

    theme.prepare_platform()
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    today = int(sys.argv[sys.argv.index("--today") + 1]) if "--today" in sys.argv else 18
    lib = Library(Path(tempfile.mkdtemp(prefix="reader-start-")))
    dl = Path.home() / "Downloads"
    src = next(iter(dl.glob("*.epub")), None) or next(iter(dl.glob("*.pdf")))
    entry = lib.add(src)
    win = QMainWindow()
    win.resize(1280, 800)
    theme.style(win, "QMainWindow {{ background: {bg2}; }}")
    win.show()
    theme.titlebar(win)
    dlg = StartDialog(win, entry, lib, today_minutes=today, ai_ready="--no-ai" not in sys.argv)
    ok = dlg.exec()
    print("开始读", dlg.minutes(), "分钟" if ok else "（取消了）")
    sys.exit(0)
