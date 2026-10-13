"""书架窗口（界面稿板 1、11）：没在读的时候看到的普通窗口。

- 整个窗口只有一个主按钮：开始读（点了先弹「开始读」窗口，板 2）。
- 「在读」这本单独放大：读到哪一节、第几页、上次什么时候、划了几处。
- 书架其他书一行一本，鼠标移上去（或 Tab 到这一行）才出现「设为在读」。右键还有「在文件夹里显示」「从书架拿掉」。
  拿掉不问确认，顶上给「撤销」（拿掉本来也只是从清单里去掉，文件不动）。
- 扫描版 PDF 在行里直接写明：不能划线、AI 看不到正文。文件不见了也直接写，给「重新找…」。
- 添加书：按钮（Ctrl+O）或直接把 PDF / EPUB 拖进窗口。打开书在后台线程里做。
- 右栏：今天 N / 45（读 lock 记的阅读统计 status.json）、这周 7 根柱子、读书笔记、AI 登录状态、亮 / 暗。
- reader 专用的 Claude 账号没登录：登录块一直留在顶上（login.py），不弹窗。
- 出错、紧急退出、强行退出：顶上留一行字，点 × 才走（不用会自己消失的提示条）。

信号：start_requested(BookEntry, 分钟) —— 「开始读」窗口点了开始。app.py 接着打开读书界面。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QMainWindow, QMenu, QScrollArea,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from reader.books import Loc
from reader.library import BOOK_EXTS, BookEntry, Library, OcrQueue
from reader.lock.state import STATUS_PATH, read_json
from reader.ui import theme
from reader.ui.login import LoginBanner, probe
from reader.ui.notes_view import NotesView, open_folder
from reader.ui.shelf_kit import (Button, Card, Cover, Dot, ElideLabel, IconLabel, Label, PBar, Seg, Tag,
                                 WeekBars, background, cap, day_header, lbl, on_theme,
                                 repaint_on_theme, row, when_label)

TARGET = 45


# ── 今天 / 这周读了多少（lock 的日报） ──────────────────────────
def read_status(path: Path = STATUS_PATH, today: date | None = None) -> dict:
    """{"today": 分钟, "target": 45, "sessions": 次数, "week": [周一…周日的分钟], "weekday": 今天是第几个}"""
    today = today or date.today()
    data = read_json(path) or {}
    days = data.get("days") or {}

    def minutes(d: date) -> int:
        rec = days.get(d.strftime("%Y-%m-%d")) or {}
        return int(float(rec.get("credited_seconds", 0) or 0) // 60)

    rec = days.get(today.strftime("%Y-%m-%d")) or {}
    monday = today - timedelta(days=today.weekday())
    return {
        "today": minutes(today),
        "target": int(rec.get("target_minutes") or TARGET),
        "sessions": int(rec.get("sessions") or 0),
        "week": [minutes(monday + timedelta(days=i)) for i in range(7)],
        "weekday": today.weekday(),
        "any": bool(days),
    }


def scan_meta(state: tuple | None) -> tuple[str, str]:
    """书架一行里扫描版那一句 + 颜色。"""
    kind, done, total = state or ("unavailable", 0, 0)     # 只给扫描版调；没有队列信息就当识别不了
    if kind == "done":
        return "扫描版 · 已识别文字", "t4"
    if kind == "unavailable":
        return "扫描版：不能划线，AI 看不到正文", "t3"
    if kind == "running":
        return f"扫描版 · 正在识别文字 {done} / {total} 页", "t3"
    if kind == "queued":
        return f"扫描版 · 排着队等识别文字（{done} / {total} 页）", "t3"
    return f"扫描版 · 文字识别停在 {done} / {total} 页，下次打开接着识别", "t3"


def scan_note(state: tuple | None) -> str:
    """「在读」卡上的提醒（识别完了就不用提醒）。"""
    if state is None or state[0] == "done":
        return ""
    kind, done, total = state
    if kind == "unavailable":
        return "扫描版：不能划线，AI 看不到正文，不出小测"
    return f"正在识别文字（{done} / {total} 页），识别到的页就能划线、问 AI"


def row_meta(entry: BookEntry, lib: Library) -> str:
    """书架一行的第二行：「第 87 / 312 页 · 28%」「读到 14%」「还没读」「读完了」。"""
    prog = lib.progress(entry.id)
    if entry.position is None or prog is None:
        return "还没读"
    if prog >= 0.995:
        return "读完了"
    if entry.kind == "pdf" and entry.pages:
        return f"第 {entry.position.doc + 1} / {entry.pages} 页 · {prog:.0%}"
    return f"读到 {prog:.0%}"


# ── 顶上一行的说明（出错 / 紧急退出 / 强行退出 / 撤销） ─────────────
class Notice(Card):
    def __init__(self, text: str, *, kind: str = "info", action: tuple[str, object] | None = None,
                 parent: QWidget | None = None):
        border = (lambda: _qcolor(theme.mix("red", "l2", 0.45))) if kind == "warn" else "l2"
        super().__init__("bg3", border, 10, parent=parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 8, 8, 8)
        h.setSpacing(12)
        self.text = Label(text, 13, 400, "t2", wrap=True)
        h.addWidget(self.text, 1)
        if action:
            b = Button(action[0], "", "sm")
            b.clicked.connect(lambda: (action[1](), self.close_me()))
            h.addWidget(b, 0, Qt.AlignVCenter)
        x = Button("", "ghost", "sm", icon_name="x")
        x.setToolTip("知道了，关掉这一行")
        x.setFixedWidth(28)
        x.clicked.connect(self.close_me)
        h.addWidget(x, 0, Qt.AlignTop)

    def close_me(self) -> None:
        self.hide()
        self.deleteLater()


def _qcolor(hexstr: str):
    from PySide6.QtGui import QColor
    return QColor(hexstr)


# ── 「在读」那张大卡 ──────────────────────────────────────────
class CurrentCard(Card):
    def __init__(self, win: "ShelfWindow", entry: BookEntry, parent: QWidget | None = None):
        super().__init__("bg3", "l1", 14, parent=parent)
        lib = win.library
        h = QHBoxLayout(self)
        h.setContentsMargins(22, 22, 22, 22)
        h.setSpacing(26)
        h.addWidget(Cover(entry.title, entry.cover, 132, 186, 20), 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(0)
        tags = [Tag(entry.kind.upper())]
        state = (win.ocr.state(entry.id) or ("unavailable", 0, entry.pages)) if entry.scanned else None
        if entry.scanned:
            tags.append(Tag("扫描版 · 已识别" if state and state[0] == "done" else "扫描版"))
        info.addLayout(row(*tags, None, spacing=8))
        info.addSpacing(6)
        title = ElideLabel(f"《{entry.title}》", 24, 600, "t1")
        title.setToolTip(f"《{entry.title}》\n{entry.path}")
        info.addWidget(title)
        info.addSpacing(2)
        place = lib.place(entry.id)
        sub = [x for x in (entry.author, f"{place['chapter']}" if place["chapter"] else "") if x]
        if sub:
            info.addWidget(cap(" · ".join(sub)))

        kv = QVBoxLayout()
        kv.setSpacing(4)
        pairs = []
        if entry.position is None:
            pairs.append(("读到", "还没开始，从头读"))
        else:
            where = place["section"] or ""
            if entry.kind == "pdf" and entry.pages:
                where = (where + " · " if where else "") + f"第 {entry.position.doc + 1} / {entry.pages} 页"
            pairs.append(("读到", where or "读过一点"))
            pairs.append(("上次", when_label(entry.last_read)))
        n_hl = len(lib.highlights(entry.id))
        pairs.append(("划线", f"{n_hl} 处" if n_hl else "还没有"))
        note = scan_note(state)
        if note:
            pairs.append(("注意", note))
        for k, v in pairs:
            r = QHBoxLayout()
            r.setSpacing(16)
            kl = Label(k, 13, 400, "t4")
            kl.setFixedWidth(26)
            r.addWidget(kl)
            vl = Label(v, 13, 400, "t2", tnum=True)
            vl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            if k == "注意":
                win.watch_ocr(entry.id, vl, scan_note)
            r.addWidget(vl, 1)
            kv.addLayout(r)
        info.addSpacing(12)
        info.addLayout(kv)

        prog = lib.progress(entry.id) or 0.0
        bar = PBar(prog)
        bar.setMinimumWidth(160)
        bar.setMaximumWidth(340)
        pr = QHBoxLayout()
        pr.setSpacing(10)
        pr.addWidget(bar, 1)
        pr.addWidget(cap(f"{prog:.0%}"))
        pr.addStretch(0)
        holder = QWidget()
        holder.setLayout(pr)
        holder.setMaximumWidth(340 + 10 + 40)
        pr.setContentsMargins(0, 0, 0, 0)
        info.addSpacing(12)
        info.addWidget(holder)
        info.addStretch(1)
        info.addSpacing(16)

        self.start_btn = Button("开始读", "pri", "lg")
        self.start_btn.setToolTip("先弹出「开始读」窗口选这次读多久，不会马上锁屏")
        self.start_btn.clicked.connect(win.open_start_dialog)
        left = win.status["target"] - win.status["today"]
        hint = cap(f"今天还差 {left} 分钟" if left > 0 else "今天读满了，想读可以加读")
        if entry.missing:
            self.start_btn.setEnabled(False)
            hint = cap("文件找不到了，开始不了", "redt")
            relink = Button("重新找…", "", "")
            relink.clicked.connect(lambda: win.relink(entry))
            info.addLayout(row(self.start_btn, hint, relink, None, spacing=14))
        else:
            info.addLayout(row(self.start_btn, hint, None, spacing=14))
        h.addLayout(info, 1)


# ── 书架一行 ────────────────────────────────────────────────
class BookRow(QFrame):
    def __init__(self, win: "ShelfWindow", entry: BookEntry, parent: QWidget | None = None):
        super().__init__(parent)
        self.win, self.entry = win, entry
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"《{entry.title}》")
        self._hover = False
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(14)
        h.addWidget(Cover(entry.title, entry.cover, 32, 44))
        text = QVBoxLayout()
        text.setSpacing(1)
        t = ElideLabel(f"《{entry.title}》", 14, 400, "t1")
        t.setToolTip(f"《{entry.title}》\n{entry.path}")
        text.addWidget(t)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        meta.addWidget(Tag(entry.kind.upper()))
        meta.addWidget(Label(row_meta(entry, win.library), 12, 400, "t4", tnum=True))
        if entry.last_read and entry.position is not None:
            meta.addWidget(Label(f"· {when_label(entry.last_read, with_time=False)}读过", 12, 400, "t4"))
        if entry.missing:
            meta.addWidget(Label("· 文件找不到了", 12, 400, "redt"))
        elif entry.scanned:
            stext, color = scan_meta(win.ocr.state(entry.id))
            sl = Label(f"· {stext}", 12, 400, color, tnum=True)
            win.watch_ocr(entry.id, sl, lambda st: "· " + scan_meta(st)[0])
            meta.addWidget(sl)
        meta.addStretch(1)
        text.addLayout(meta)
        h.addLayout(text, 1)
        if entry.missing:
            self.action = Button("重新找…", "", "sm")
            self.action.clicked.connect(lambda: win.relink(entry))
        else:
            self.action = Button("设为在读", "", "sm")
            self.action.setToolTip("换成这本；下次点「开始读」就读它")
            self.action.clicked.connect(lambda: win.set_current(entry.id))
        self.action.setFocusPolicy(Qt.TabFocus)
        h.addWidget(self.action)
        self._show_action(False)
        repaint_on_theme(self)

    def _show_action(self, on: bool) -> None:
        # 不用 hide()：隐藏会让这一行高度变化；透明 + 不响应
        eff = self.action.graphicsEffect()
        if eff is None:
            from PySide6.QtWidgets import QGraphicsOpacityEffect
            eff = QGraphicsOpacityEffect(self.action)
            self.action.setGraphicsEffect(eff)
        eff.setOpacity(1.0 if on else 0.0)
        self.action.setEnabled(on)

    def set_hover(self, on: bool) -> None:
        self._hover = on
        self._show_action(on or self.hasFocus() or self.action.hasFocus())
        self.update()

    def enterEvent(self, e) -> None:
        self.set_hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.set_hover(False)
        super().leaveEvent(e)

    def focusInEvent(self, e) -> None:
        self._show_action(True)
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e) -> None:
        QTimer.singleShot(0, lambda: self._show_action(self._hover or self.action.hasFocus()))
        self.update()
        super().focusOutEvent(e)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.action.click() if self.action.isEnabled() else None
            return
        if e.key() == Qt.Key_Delete:
            self.win.remove_book(self.entry)
            return
        super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, _e) -> None:
        if not self.entry.missing:
            self.win.set_current(self.entry.id)

    def contextMenuEvent(self, e) -> None:
        m = QMenu(self)
        a1 = m.addAction("设为在读")
        a1.setEnabled(not self.entry.missing)
        a2 = m.addAction("在文件夹里显示")
        a2.setEnabled(not self.entry.missing)
        a3 = m.addAction("重新找这个文件…")
        m.addSeparator()
        a4 = m.addAction("从书架拿掉（文件不动）")
        got = m.exec(e.globalPos())
        if got is a1:
            self.win.set_current(self.entry.id)
        elif got is a2:
            open_folder(Path(self.entry.path))
        elif got is a3:
            self.win.relink(self.entry)
        elif got is a4:
            self.win.remove_book(self.entry)

    def paintEvent(self, _e) -> None:
        if not (self._hover or self.hasFocus()):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme.color("focus"), 1) if self.hasFocus() and not self._hover else Qt.NoPen)
        p.setBrush(theme.color("bg4"))
        p.drawRoundedRect(r, 9, 9)
        p.end()


# ── 空书架的拖放区 / 拖进来时的提示 ─────────────────────────────
class DropZone(Card):
    def __init__(self, win: "ShelfWindow", parent: QWidget | None = None):
        super().__init__(None, "l3", 14, border_w=1.5, dashed=True, parent=parent)
        v = QVBoxLayout(self)
        v.setSpacing(10)
        v.addStretch(1)
        v.addWidget(IconLabel("dl", "t4", 26), 0, Qt.AlignHCenter)
        v.addWidget(Label("把 PDF 或 EPUB 拖到这里", 16, 400, "t1"), 0, Qt.AlignHCenter)
        v.addWidget(cap("书架还是空的。放进来的第一本会自动设成「在读」。"), 0, Qt.AlignHCenter)
        v.addSpacing(6)
        b = Button("添加书…", "", "", icon_name="plus")
        b.clicked.connect(win.add_books_dialog)
        v.addWidget(b, 0, Qt.AlignHCenter)
        v.addStretch(1)


class DragOverlay(QWidget):
    """拖着文件经过窗口时：主区域盖一层，写「松手就加进书架」。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text = "松手就加进书架"
        self.hide()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(16, 16, -16, -16)
        bg = theme.color("bg2")
        bg.setAlphaF(0.92)
        p.setBrush(bg)
        pen = QPen(theme.color("acc"), 1.5)
        pen.setStyle(Qt.CustomDashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.drawRoundedRect(r, 14, 14)
        p.setPen(theme.color("t1"))
        p.setFont(theme.font(16, 400))
        p.drawText(r, Qt.AlignCenter, self.text)
        p.end()


# ── 右栏 ────────────────────────────────────────────────────
class NoteRow(QFrame):
    def __init__(self, win: "ShelfWindow", path: Path, left: str, right: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.win, self.path = win, path
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setToolTip(str(path))
        self._hover = False
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 7, 0, 7)
        h.setSpacing(10)
        h.addWidget(ElideLabel(left, 13, 400, "t2"), 1)
        h.addWidget(Label(right, 13, 400, "t4", tnum=True))
        repaint_on_theme(self)

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.win.open_note(self.path)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.win.open_note(self.path)
        else:
            super().keyPressEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        if self._hover or self.hasFocus():
            p.fillRect(self.rect(), theme.color("bg3"))
        p.fillRect(0, self.height() - 1, self.width(), 1, theme.color("l1"))
        p.end()


# ── 窗口 ────────────────────────────────────────────────────
class ShelfWindow(QMainWindow):
    start_requested = Signal(object, int)      # (BookEntry, 分钟)

    def __init__(self, library: Library, *, status_path: Path = STATUS_PATH,
                 prober=probe, launcher=None, check_login: bool = True, ocr: OcrQueue | None = None):
        super().__init__()
        self.library = library
        self.ocr = ocr or OcrQueue(library)
        self._ocr_watch: list[tuple[str, Label, object]] = []
        self._ocr_seen: dict[str, str] = {}
        self._ocr_timer = QTimer(self)        # 识别文字的进度：有活在做时每秒更新那几行字
        self._ocr_timer.setInterval(1000)
        self._ocr_timer.timeout.connect(self._ocr_tick)
        self.status_path = Path(status_path)
        self.status = read_status(self.status_path)
        self.setWindowTitle("reader")
        self.setMinimumSize(960, 640)
        self.resize(1280, 800)
        self.setAcceptDrops(True)
        self._notes_expanded = False
        self._adding: list[str] = []
        self._hint_text = "也可以直接把文件拖进来"

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.shelf_page = QWidget()
        self.stack.addWidget(self.shelf_page)
        self.notes_view = NotesView(library, today_fn=lambda: (self.status["today"], self.status["target"]))
        self.notes_view.back.connect(self.show_shelf)
        self.stack.addWidget(self.notes_view)
        theme.style(self, "QMainWindow {{ background: {bg2}; }}")

        outer = QHBoxLayout(self.shelf_page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 主区域
        self.main = QWidget()
        self.main.setFocusPolicy(Qt.ClickFocus)    # 点空白处焦点落在这里；Tab 不停
        self.main_lay = QVBoxLayout(self.main)
        self.main_lay.setContentsMargins(36, 28, 36, 20)
        self.main_lay.setSpacing(0)
        self.notices = QVBoxLayout()
        self.notices.setSpacing(10)
        self.main_lay.addLayout(self.notices)
        self.banner = LoginBanner(library, prober=prober, launcher=launcher)
        self.banner.settings_requested.connect(self.open_ai_settings)
        self.main_lay.addWidget(self.banner)
        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        self.main_lay.addLayout(self.body, 1)
        outer.addWidget(self.main, 1)
        self.overlay = DragOverlay(self.main)

        # 右栏
        side = Card("bg1", None, 0)
        side.setFixedWidth(340)
        sl = QHBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        edge = Card("l1", None, 0)
        edge.setFixedWidth(1)
        sl.addWidget(edge)
        self.side = QVBoxLayout()
        self.side.setContentsMargins(24, 28, 24, 20)
        self.side.setSpacing(0)
        sl.addLayout(self.side, 1)
        self.side_top = QVBoxLayout()
        self.side_top.setSpacing(0)
        self.side.addLayout(self.side_top, 1)
        self.ai_settings_btn = Button('AI 设置', 'ghost', 'sm')
        self.ai_settings_btn.clicked.connect(self.open_ai_settings)
        self.side.addWidget(self.ai_settings_btn)
        self.side.addSpacing(8)
        self.ai_dot = Dot("t5")
        self.ai_text = cap("正在看 AI 登录了没有…")
        self.seg = Seg([("light", "亮"), ("dark", "暗")], theme.current())
        self.seg.setToolTip("换成亮色 / 暗色（会记住）")
        self.seg.value_changed.connect(lambda k: theme.set_theme(k))
        self.side.addLayout(row(self.ai_dot, self.ai_text, None, self.seg, spacing=7))
        outer.addWidget(side)

        self.banner.state_changed.connect(self._on_login_state)
        on_theme(self, self._on_theme)
        QShortcut(QKeySequence.Open, self, activated=self.add_books_dialog)

        self._clock = QTimer(self)          # 过了半夜、统计变了：每分钟重读一次
        self._clock.setInterval(60_000)
        self._clock.timeout.connect(self.refresh_status)
        self._clock.start()

        self.refresh()
        if check_login:
            QTimer.singleShot(0, lambda: self.banner.recheck(quiet=True))

    # ── 刷新 ────────────────────────────────────────────
    def refresh(self) -> None:
        self.status = read_status(self.status_path)
        self._ocr_watch = []
        self.current_card = None
        self.rows = []
        _clear(self.body)
        books = self.library.books()
        cur = self.library.current()
        if not books:
            self.body.addSpacing(26 if self.banner.isVisible() else 0)
            self.body.addWidget(DropZone(self), 1)
        else:
            if self.banner.isVisible():
                self.body.addSpacing(26)
            if cur is not None:
                self.body.addWidget(lbl("在读"))
                self.body.addSpacing(10)
                self.current_card = CurrentCard(self, cur)
                self.body.addWidget(self.current_card)
                self.body.addSpacing(26)
            others = [b for b in books if cur is None or b.id != cur.id]
            head = QHBoxLayout()
            head.setSpacing(10)
            head.addWidget(lbl(f"书架 · {len(books)} 本"))
            head.addStretch(1)
            self.hint = cap(self._hint_text)
            head.addWidget(self.hint)
            add = Button("添加书…", "", "sm", icon_name="plus")
            add.setToolTip("选 PDF 或 EPUB（Ctrl+O）")
            add.clicked.connect(self.add_books_dialog)
            head.addWidget(add)
            self.body.addLayout(head)
            self.body.addSpacing(6)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            theme.style(scroll, "QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}")
            inner = QWidget()
            il = QVBoxLayout(inner)
            il.setContentsMargins(0, 0, 0, 0)
            il.setSpacing(0)
            self.rows: list[BookRow] = []
            for b in others:
                r = BookRow(self, b)
                self.rows.append(r)
                il.addWidget(r)
            if not others:
                il.addWidget(cap("书架上就这一本。再放几本进来，读完这本好接着读。"))
            il.addStretch(1)
            scroll.setWidget(inner)
            self.body.addWidget(scroll, 1)
        self._build_side()
        QTimer.singleShot(0, self._restore_focus)

    def _restore_focus(self) -> None:
        """重建后原来有焦点的控件没了，Qt 会把焦点随手交给下一个（常常是右下角的亮 / 暗）：交回「开始读」。"""
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        card = self.current_card
        if not self.isVisible() or self.stack.currentWidget() is not self.shelf_page:
            return
        if fw is None or fw is self or self.seg.isAncestorOf(fw):
            try:
                if card is not None and card.start_btn.isEnabled():
                    card.start_btn.setFocus()
                else:
                    self.main.setFocus()        # 没有主按钮时放在主区域上（不显示焦点圈）
            except RuntimeError:
                pass

    # ── 扫描版识别文字的进度 ─────────────────────────────
    def watch_ocr(self, book_id: str, label: Label, fmt) -> None:
        self._ocr_watch.append((book_id, label, fmt))
        if not self._ocr_timer.isActive():
            self._ocr_timer.start()

    def _ocr_tick(self) -> None:
        changed = False
        for bid, label, fmt in list(self._ocr_watch):
            st = self.ocr.state(bid)
            kind = st[0] if st else ""
            if self._ocr_seen.get(bid) not in (None, kind) and kind == "done":
                changed = True
            self._ocr_seen[bid] = kind
            try:
                label.setText(fmt(st))
            except RuntimeError:
                pass
        if changed:
            self.refresh()
        elif not self.ocr.busy():
            self._ocr_timer.stop()

    def refresh_status(self) -> None:
        new = read_status(self.status_path)
        if new != self.status and self.stack.currentWidget() is self.shelf_page:
            self.refresh()

    def _build_side(self) -> None:
        _clear(self.side_top)
        s = self.status
        lay = self.side_top
        lay.addWidget(lbl(f"今天 · {day_header(date.today())}"))
        lay.addSpacing(10)
        big = QHBoxLayout()
        big.setSpacing(4)
        big.addWidget(Label(str(s["today"]), 40, 600, "t1", tnum=True), 0, Qt.AlignBottom)
        small = Label(f"/ {s['target']} 分钟", 16, 400, "t4", tnum=True)
        small.setContentsMargins(0, 0, 0, 3)
        big.addWidget(small, 0, Qt.AlignBottom)
        big.addStretch(1)
        lay.addLayout(big)
        lay.addSpacing(14)
        full = s["today"] >= s["target"]
        lay.addWidget(PBar(s["today"] / s["target"] if s["target"] else 0, "green" if full else "acc"))
        lay.addSpacing(8)
        lay.addWidget(cap(self._today_text(), wrap=True))

        if s["any"]:
            lay.addSpacing(26)
            lay.addWidget(lbl("这周"))
            lay.addSpacing(10)
            wk = WeekBars()
            wk.set_week([m / s["target"] for m in s["week"]], s["weekday"])
            lay.addWidget(wk)
            lay.addSpacing(8)
            lay.addWidget(cap(self._week_text(), wrap=True))

        lay.addSpacing(26)
        lay.addWidget(lbl("读书笔记"))
        lay.addSpacing(4)
        notes = self.library.notes()
        if not notes:
            lay.addSpacing(2)
            lay.addWidget(cap("每读完一次，AI 写一页，放在这里。", wrap=True))
        else:
            shown = notes if self._notes_expanded else notes[:3]
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            for p in shown:
                left, right = self._note_label(p)
                bl.addWidget(NoteRow(self, p, left, right))
            bl.addStretch(1)
            if self._notes_expanded and len(notes) > 6:
                sc = QScrollArea()
                sc.setWidgetResizable(True)
                sc.setFrameShape(QFrame.NoFrame)
                theme.style(sc, "QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}")
                sc.setWidget(box)
                lay.addWidget(sc, 1)
            else:
                lay.addWidget(box)
            if len(notes) > 3:
                more = Button(("收起" if self._notes_expanded else f"全部 {len(notes)} 篇 ›"), "ghost", "sm")
                more.setStyleSheet(more.styleSheet() + "QPushButton { padding: 0; }")
                more.clicked.connect(self._toggle_notes)
                lay.addSpacing(4)
                lay.addLayout(row(more, None))
        lay.addStretch(1)

    def _today_text(self) -> str:
        s = self.status
        today = date.today().strftime("%Y-%m-%d")
        recs = self.library.sessions(today)
        spans = []
        for r in recs:
            try:
                a, b = datetime.fromisoformat(r["started"]), datetime.fromisoformat(r["ended"])
                spans.append(f"{a:%H:%M}–{b:%H:%M}")
            except (KeyError, ValueError):
                pass
        n = max(len(recs), s["sessions"])
        if s["today"] <= 0 and not n:
            return f"每天读满 {s['target']} 分钟。什么时候读你自己定。"
        head = "读满了 · " if s["today"] >= s["target"] else ""
        times = f"（{'、'.join(spans)}）" if spans else ""
        return f"{head}今天读了 {n or 1} 次{times}"

    def _week_text(self) -> str:
        s = self.status
        full = sum(1 for m in s["week"][:s["weekday"] + 1] if m >= s["target"])
        bits = [f"读满 {full} 天" if full else "这周还没有读满的一天"]
        y = s["weekday"] - 1
        if y >= 0 and 0 < s["week"][y] < s["target"]:
            bits.append(f"周{WeekBars.NAMES[y]}读了 {s['week'][y]} 分钟")
        return " · ".join(bits)

    def _note_label(self, p: Path) -> tuple[str, str]:
        rec = self.library.session_for_note(p)
        book = p.parent.name
        extra = ""
        if rec:
            e = self.library.get(rec.get("book_id", ""))
            if e:
                book = e.title
            extra = rec.get("section") or ""
        left = f"《{book}》{extra}"
        right = when_label(datetime.fromtimestamp(p.stat().st_mtime))
        return left, right

    def _toggle_notes(self) -> None:
        self._notes_expanded = not self._notes_expanded
        self._build_side()

    # ── 登录状态 ────────────────────────────────────────
    def open_ai_settings(self) -> None:
        from reader.ui.ai_settings import AISettingsDialog
        AISettingsDialog(self).exec()
        self.banner.recheck()

    def _on_login_state(self, state: str) -> None:
        color, text = self.banner.status_line()
        self.ai_dot.set_color(color)
        self.ai_text.setText(text)
        self.refresh()

    # ── 主题 ────────────────────────────────────────────
    def _on_theme(self) -> None:
        self.seg.set_value(theme.current())
        theme.titlebar(self)
        self.refresh()          # 封面阴影、分割线这些一起重建最省事

    # ── 动作 ────────────────────────────────────────────
    def open_start_dialog(self) -> bool:
        """弹「开始读」。点了开始就发 start_requested，返回 True。"""
        from reader.ui.start_dialog import StartDialog
        cur = self.library.current()
        if cur is None:
            self.add_notice("书架上还没有书。先把 PDF 或 EPUB 拖进来，再点开始读。")
            return False
        if cur.missing:
            self.add_notice(f"《{cur.title}》的文件找不到了：{cur.path}。点「重新找…」指到新位置再开始。",
                            kind="warn")
            return False
        self.show_shelf()
        self.status = read_status(self.status_path)
        ai = None if self.banner.state == "checking" else self.banner.logged_in()
        dlg = StartDialog(self, cur, self.library, today_minutes=self.status["today"],
                          target=self.status["target"], ai_ready=ai,
                          ocr_state=self.ocr.state(cur.id) if cur.scanned else None)
        self._dialog = dlg
        ok = dlg.exec() == StartDialog.Accepted
        self._dialog = None
        if ok and dlg.minutes():
            self.start_requested.emit(cur, int(dlg.minutes()))
            return True
        return False

    def set_current(self, book_id: str) -> None:
        self.library.set_current(book_id)
        self.refresh()

    def remove_book(self, entry: BookEntry) -> None:
        self.library.remove(entry.id)
        self.refresh()
        self.add_notice(f"《{entry.title}》从书架拿掉了（文件没动）。",
                        action=("撤销", lambda: self._undo_remove(entry)))

    def _undo_remove(self, entry: BookEntry) -> None:
        was_current = self.library.current() is None
        try:
            self.library.add(Path(entry.path))
        except ValueError as e:
            self.add_notice(f"《{entry.title}》放不回来：{e}", kind="warn")
            return
        if was_current:
            self.library.set_current(entry.id)
        self.refresh()

    def relink(self, entry: BookEntry) -> None:
        ext = Path(entry.path).suffix.lower()
        path, _ = QFileDialog.getOpenFileName(self, f"《{entry.title}》现在在哪", self._last_dir(),
                                              f"同一本书 (*{ext})")
        if not path:
            return
        try:
            self.library.relink(entry.id, Path(path))
        except ValueError as e:
            self.add_notice(f"没指过去：{e}", kind="warn")
        self.refresh()

    def _last_dir(self) -> str:
        d = self.library.setting("last_add_dir")
        return d if d and Path(d).is_dir() else str(Path.home() / "Downloads")

    def add_books_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "添加书", self._last_dir(), "书 (*.pdf *.epub)")
        if paths:
            self.library.set_setting("last_add_dir", str(Path(paths[0]).parent))
            self.add_books([Path(p) for p in paths])

    def add_books(self, paths: list[Path]) -> None:
        """在后台一本本打开、加进书架。行上方那句话说明正在加哪本。"""
        for p in paths:
            if p.suffix.lower() not in BOOK_EXTS:
                self.add_notice(f"「{p.name}」没加：只能放 PDF 或 EPUB。", kind="warn")
                continue
            before = {b.id for b in self.library.books()}
            self._adding.append(p.name)
            self._set_hint()

            def done(res, err, p=p, before=before):
                self._adding.remove(p.name)
                if err is not None:
                    self.add_notice(f"「{p.name}」没加上：{err}", kind="warn")
                    self._set_hint()
                    return
                self._hint_text = (f"《{res.title}》已经在书架上了" if res.id in before
                                   else "也可以直接把文件拖进来")
                if res.scanned:
                    self.ocr.enqueue(res)       # 扫描版：后台用本机文字识别补上文字
                self.refresh()
                self._set_hint()
            background(lambda p=p: self.library.add(p), done)

    def _set_hint(self) -> None:
        if not hasattr(self, "hint"):
            return
        try:
            if self._adding:
                self.hint.setText(f"正在加《{Path(self._adding[0]).stem[:24]}》…"
                                  + (f" 还有 {len(self._adding) - 1} 本" if len(self._adding) > 1 else ""))
            else:
                self.hint.setText(self._hint_text)
        except RuntimeError:
            pass

    def add_notice(self, text: str, *, kind: str = "info", action=None) -> Notice:
        n = Notice(text, kind=kind, action=action)
        self.notices.addWidget(n)
        self.notices.setContentsMargins(0, 0, 0, 16)
        return n

    def open_note(self, path: Path) -> None:
        self.notes_view.show_note_file(path)
        self.stack.setCurrentWidget(self.notes_view)

    def show_session(self, entry, summary: dict, started: datetime, ended: datetime) -> None:
        """读完回来：显示这次的总结 + 读书笔记（板 10②）。"""
        self.status = read_status(self.status_path)
        self.notes_view.show_session(entry, summary, started, ended, fresh=True)
        self.stack.setCurrentWidget(self.notes_view)

    def show_shelf(self) -> None:
        if self.stack.currentWidget() is not self.shelf_page:
            self.stack.setCurrentWidget(self.shelf_page)
            self.refresh()

    # ── 拖放 ────────────────────────────────────────────
    @staticmethod
    def _book_urls(e) -> list[Path]:
        md = e.mimeData()
        if not md.hasUrls():
            return []
        return [Path(u.toLocalFile()) for u in md.urls()
                if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in BOOK_EXTS]

    def dragEnterEvent(self, e) -> None:
        paths = self._book_urls(e)
        if paths:
            e.acceptProposedAction()
            n = len(paths)
            self.overlay.text = "松手就加进书架" if n == 1 else f"松手就把这 {n} 本加进书架"
            self.overlay.setGeometry(self.main.rect())
            self.overlay.raise_()
            self.overlay.show()
        else:
            e.ignore()

    def dragLeaveEvent(self, _e) -> None:
        self.overlay.hide()

    def dropEvent(self, e) -> None:
        self.overlay.hide()
        paths = self._book_urls(e)
        if paths:
            e.acceptProposedAction()
            self.show_shelf()
            self.add_books(paths)

    # ── 窗口 ────────────────────────────────────────────
    def changeEvent(self, e) -> None:
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.banner.on_window_activated()
            self.refresh_status()
        super().changeEvent(e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        theme.titlebar(self)
        card = getattr(self, "current_card", None)
        try:
            if card is not None and card.start_btn.isEnabled():
                card.start_btn.setFocus()     # 键盘焦点先落在唯一的主按钮上（回车 = 开始读）
        except RuntimeError:
            pass

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()


def _clear(lay) -> None:
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif it.layout() is not None:
            _clear(it.layout())


if __name__ == "__main__":
    # 演示：临时书架 + ~/Downloads 里的几本真书 + 一份假的今天 / 这周读书数。真的去查 AI 登录（只读）。
    # 点「开始读」只打印选了几分钟，不会进锁屏。不碰真的书架和日报。
    import json
    import sys
    import tempfile

    from PySide6.QtWidgets import QApplication

    theme.prepare_platform()
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    tmp = Path(tempfile.mkdtemp(prefix="reader-shelf-"))
    lib = Library(tmp / "data")
    dl = Path.home() / "Downloads"
    for pat in ("*.epub", "*.pdf"):
        p = next(iter(dl.glob(pat)), None)
        if p is not None:
            e = lib.add(p)
            if pat == "*.pdf":
                lib.save_position(e.id, Loc(40, 0))
    cur = lib.current()
    if cur:
        lib.save_position(cur.id, Loc(12, 3))
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    days = {}
    for i, m in enumerate([45, 47, 31, 18, 0, 0, 0][:today.weekday() + 1]):
        d = monday + timedelta(days=i)
        days[d.strftime("%Y-%m-%d")] = {"credited_seconds": m * 60, "sessions": 1, "target_minutes": 45}
    status = tmp / "status.json"
    status.write_text(json.dumps({"days": days}), "utf-8")
    w = ShelfWindow(lib, status_path=status)
    w.start_requested.connect(lambda e, m: print(f"会开始读《{e.title}》{m} 分钟（演示不进锁屏）"))
    w.show()
    sys.exit(app.exec())
