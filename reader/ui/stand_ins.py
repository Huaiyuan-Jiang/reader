"""读书界面（D）的替身零件：B 的 BookView、C 的 AiPanel、A 的 Library 还在并行写，
D 的演示和测试先用这里的最小替身，接口照 docs/UI_PLAN.md。

**合并后**：演示会自动优先用真零件（reader.ui.bookview / reader.ui.ai_panel 能导入就用真的）；
测试继续用替身（不调真 AI、不依赖别人的实现细节）。这个文件只给演示和测试用，正式程序不导入它。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

from . import theme
from .cards import button, label, no_focus, on_theme, restyle, rgba


# ── A · Library ─────────────────────────────────────────────
@dataclass
class BookEntry:
    id: str
    path: str
    title: str
    author: str = ""
    kind: str = "epub"
    position: object = None
    added: str = ""
    last_read: str | None = None
    scanned: bool = False
    missing: bool = False


class MemoryLibrary:
    """Library 的内存版：记下每次调用，笔记写到临时目录。"""

    def __init__(self, notes_dir: Path | None = None):
        self._books: dict[str, BookEntry] = {}
        self.positions: dict[str, object] = {}
        self._hl: dict[str, list] = {}
        self._quiz: dict[str, set] = {}
        self.saved_notes: list[tuple[str, str, datetime]] = []
        self.calls: list[tuple] = []
        self.notes_dir = notes_dir

    def add(self, path: Path) -> BookEntry:
        path = Path(path)
        title = re.split(r"\s+--\s+|[_(（]", path.stem)[0].strip() or path.stem
        e = BookEntry(id=f"b{len(self._books) + 1}", path=str(path), title=title,
                      kind=path.suffix.lower().lstrip("."))
        self._books[e.id] = e
        return e

    def books(self):
        return list(self._books.values())

    def save_position(self, book_id, loc):
        self.calls.append(("save_position", book_id, loc))
        self.positions[book_id] = loc

    def highlights(self, book_id):
        return list(self._hl.get(book_id, []))

    def save_highlights(self, book_id, items):
        self.calls.append(("save_highlights", book_id, list(items)))
        self._hl[book_id] = list(items)

    def quiz_done(self, book_id):
        return set(self._quiz.get(book_id, set()))

    def mark_quiz_done(self, book_id, section):
        self.calls.append(("mark_quiz_done", book_id, section))
        self._quiz.setdefault(book_id, set()).add(section)

    def save_note(self, book_id, markdown, when):
        self.calls.append(("save_note", book_id))
        self.saved_notes.append((book_id, markdown, when))
        if self.notes_dir is None:
            return Path(f"笔记/{book_id}/{when:%Y-%m-%d} 第 {len(self.saved_notes)} 次.md")
        p = Path(self.notes_dir) / f"{book_id}-{len(self.saved_notes)}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(markdown, "utf-8")
        return p


# ── B · BookView ────────────────────────────────────────────
class FakeBookView(QWidget):
    """够演示和测试用的书页：EPUB 按块粗分页，←→ / 空格 / 滚轮翻页，挡节末、标出处、蒙层。"""

    activity = Signal()
    location_changed = Signal(object)
    ask_ai = Signal(str, object)
    highlights_changed = Signal()
    barrier_hit = Signal(int)

    PAGE_CHARS = 700

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.calls: list[tuple] = []
        self.book = None
        self.units: list[tuple] = []      # [(Loc, Block)]
        self.pages: list[int] = [0]       # 每页第一个块在 units 里的下标
        self.page = 0
        self.barrier: int | None = None
        self.blocked = False
        self.dimmed = False
        self._hls: list[dict] = []
        self._src = None
        self._back_page: int | None = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_StyledBackground, True)
        theme.style(self, "FakeBookView {{ background: {paper}; }}")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.text = QTextBrowser(self)
        self.text.setFrameShape(QFrame.NoFrame)
        self.text.setFocusPolicy(Qt.NoFocus)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text.viewport().installEventFilter(self)
        restyle(self.text, lambda: f"QTextBrowser {{ background: {theme.tok('paper')}; border: 0; }}")
        col.addWidget(self.text, 1)
        self.msg = label("", 13, "acc", center=True)
        self.msg.hide()
        col.addWidget(self.msg)
        self.bar = QWidget(self)
        self.bar.setFixedHeight(40)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(16, 0, 16, 0)
        self.bar_l = label("目录　　高亮 0", 12, "t4", wrap=False)
        self.bar_c = label("", 12, "t3", wrap=False, tnum=True)
        self.bar_r = label("Aa", 13, "t4", wrap=False)
        bl.addWidget(self.bar_l)
        bl.addStretch(1)
        bl.addWidget(self.bar_c)
        bl.addStretch(1)
        bl.addWidget(self.bar_r)
        col.addWidget(self.bar)
        self.pill = QFrame(self)
        self.pill.setObjectName("pill")
        restyle(self.pill, lambda: f"QFrame#pill {{ background: {theme.tok('bg3')}; border: 1px solid "
                                   f"{theme.tok('l2')}; border-radius: 17px; }}")
        pl = QHBoxLayout(self.pill)
        pl.setContentsMargins(14, 4, 6, 4)
        self.pill_text = label("", 13, "t2", wrap=False, tnum=True)
        self.pill_btn = button("", "", height=28)
        self.pill_btn.clicked.connect(self._go_back)
        pl.addWidget(self.pill_text)
        pl.addWidget(self.pill_btn)
        self.pill.hide()
        self._dim = QWidget(self)
        self._dim.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self._dim.hide()
        self._dim.paintEvent = self._paint_dim
        on_theme(self, lambda w: w._render())

    # 接口
    def open(self, book, loc, highlights):
        self.calls.append(("open", loc))
        self.book = book
        self._hls = list(highlights or [])
        from ..books import Loc
        from ..books.model import Block
        if book.kind == "epub":
            self.units = list(book.iter_blocks(Loc(0, 0), book.end()))
        else:
            self.units = [(Loc(p), Block("p", book.page_text(p) or "（这一页没有文字层）"))
                          for p in range(book.pages)]
        self._paginate()
        self.page = self._page_of(loc) if loc is not None else 0
        self._render()
        self.location_changed.emit(self.current_loc())

    def _paginate(self):
        self.pages, n = [0], 0
        for i, (_l, b) in enumerate(self.units):
            size = max(len(b.text), 34) + (80 if b.kind.startswith("h") else 20)
            if n + size > self.PAGE_CHARS and n > 0:
                self.pages.append(i)
                n = 0
            n += size

    def _page_of(self, loc):
        idx = 0
        for i, (l, _b) in enumerate(self.units):
            if l <= loc:
                idx = i
            else:
                break
        p = 0
        for k, start in enumerate(self.pages):
            if start <= idx:
                p = k
        return p

    def current_loc(self):
        if not self.units:
            from ..books import Loc
            return Loc(0, 0)
        return self.units[self.pages[self.page]][0]

    def goto(self, loc):
        self.calls.append(("goto", loc))
        self.page = self._page_of(loc)
        self._render()
        self.location_changed.emit(self.current_loc())

    def set_barrier(self, idx):
        self.calls.append(("set_barrier", idx))
        self.barrier = idx
        self.blocked = False
        self.msg.hide()

    def mark_source(self, loc, quote):
        self.calls.append(("mark_source", loc, quote))
        self._back_page = self.page
        self._src = quote
        self.goto(loc)
        self.pill_text.setText(f"跳到了原文 · {self.page_label(loc)}")
        self.pill_btn.setText(f"回到刚才的第 {self._back_page + 1} 页")
        self.pill.adjustSize()
        self.pill.show()
        self._place()

    def clear_source(self):
        self.calls.append(("clear_source",))
        self._src = None
        self.pill.hide()
        self._render()

    def highlights(self):
        return list(self._hls)

    def set_dimmed(self, on):
        self.calls.append(("set_dimmed", bool(on)))
        self.dimmed = bool(on)
        self._dim.setVisible(self.dimmed)
        self._place()

    def page_label(self, loc):
        return f"第 {self._page_of(loc) + 1} 页"

    # 翻页
    def turn(self, step: int):
        self.activity.emit()
        target = self.page + step
        if target < 0 or target >= len(self.pages):
            return
        if step > 0 and self.barrier is not None and self.book is not None:
            sec = self.book.sections[self.barrier]
            first = self.units[self.pages[target]][0]
            if first >= sec.end:
                self.blocked = True
                self.msg.setText("先答完左边这道题")
                self.msg.show()
                self.barrier_hit.emit(self.barrier)
                return
        self.page = target
        self._render()
        self.location_changed.emit(self.current_loc())

    def _go_back(self):
        if self._back_page is not None:
            self.page = self._back_page
            self._back_page = None
            self.pill.hide()
            self._render()
            self.location_changed.emit(self.current_loc())

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key_Right, Qt.Key_Space, Qt.Key_PageDown):
            self.turn(1)
        elif e.key() in (Qt.Key_Left, Qt.Key_PageUp):
            self.turn(-1)
        else:
            super().keyPressEvent(e)

    def eventFilter(self, obj, ev):  # noqa: N802
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Wheel:
            self.turn(1 if ev.angleDelta().y() < 0 else -1)
            return True
        if ev.type() == QEvent.MouseButtonPress:
            self.activity.emit()
            self.setFocus()
        return False

    # 画
    def _render(self):
        if not self.units:
            return
        a = self.pages[self.page]
        b = self.pages[self.page + 1] if self.page + 1 < len(self.pages) else len(self.units)
        ink, ink2 = theme.tok("ink"), theme.tok("ink2")
        parts = []
        for loc, blk in self.units[a:b]:
            t = blk.text.replace("&", "&amp;").replace("<", "&lt;")
            if blk.kind.startswith("h"):
                parts.append(f'<p style="font-size:24px;font-weight:600;color:{ink};margin:0 0 22px 0;'
                             f'line-height:140%">{t}</p>')
            elif blk.kind == "img":
                parts.append(f'<p style="color:{ink2};font-size:13px" align="center">［图］{t}</p>')
            else:
                parts.append(f'<p style="margin:0 0 15px 0;text-indent:36px;line-height:{theme.book_line_height_percent():.0f}%"'
                             f' align="justify">{t}</p>')
        doc = QTextDocument()
        f = theme.font(18, 400)
        doc.setDefaultFont(f)
        doc.setDefaultStyleSheet(f"p {{ color: {ink}; }}")
        doc.setHtml(f'<div style="color:{ink}">{"".join(parts)}</div>')
        doc.setDocumentMargin(0)
        self.text.setDocument(doc)
        self.text.document().setDefaultFont(f)
        if self._src:
            self._mark(self._src)
        sec = self.book.section_at(self.current_loc()) if self.book else None
        self.bar_l.setText(f"目录　　高亮 {len(self._hls)}")
        pct = int(100 * self.page / max(1, len(self.pages) - 1))
        self.bar_c.setText(f"‹　{self.page + 1} / {len(self.pages)}　›　　{(sec.title if sec else '')[:14]} · {pct}%")
        self._place()

    def _mark(self, quote: str):
        doc = self.text.document()
        key = quote.strip()[:12]
        cur = doc.find(key)
        if cur.isNull():
            return
        cur.setPosition(cur.selectionStart())
        cur.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, min(len(quote), 60))
        fmt = QTextCharFormat()
        fmt.setBackground(theme.color("src"))
        fmt.setUnderlineStyle(QTextCharFormat.SingleUnderline)
        fmt.setUnderlineColor(theme.color("acc"))
        cur.mergeCharFormat(fmt)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._place()

    def _place(self):
        w, h = self.width(), self.height()
        colw = min(640, w - 80)
        side = max(40, (w - colw) // 2)
        self.text.setViewportMargins(0, 0, 0, 0)
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.text.setContentsMargins(0, 0, 0, 0)
        self.text.document().setTextWidth(colw)
        self.text.setStyleSheet(f"QTextBrowser {{ background: {theme.tok('paper')}; border: 0;"
                                f" padding: 34px {side}px 0 {side}px; }}")
        self.pill.move((w - self.pill.width()) // 2, 14)
        self.pill.raise_()
        self._dim.setGeometry(0, 0, w, h)
        if self.dimmed:
            self._dim.raise_()

    def _paint_dim(self, _e):
        p = QPainter(self._dim)
        p.fillRect(self._dim.rect(), theme.color("paper", 0.62))


# ── C · AiPanel ─────────────────────────────────────────────
class FakeAiPanel(QWidget):
    """左栏替身：顶上「问 AI · 看得到」、一段示例对话、小测卡（4 选 1，先选再「就选 X」）、输入框样子。"""

    activity = Signal()
    jump_to = Signal(object, str)
    quiz_finished = Signal(int, bool)
    collapsed_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None, sample_chat: bool = True):
        super().__init__(parent)
        self.calls: list[tuple] = []
        self.companion = None
        self.quiz = None
        self._pick: int | None = None
        self.setFixedWidth(340)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("aipanel")
        restyle(self, lambda: f"QWidget#aipanel {{ background: {theme.tok('bg2')};"
                              f" border-right: 1px solid {theme.tok('l1')}; }}")
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 10, 14, 14)
        col.setSpacing(0)
        head = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.setSpacing(1)
        trow = QHBoxLayout()
        trow.setSpacing(7)
        trow.addWidget(label("问 AI", 14, "t1", 600, wrap=False))
        self.dot = QLabel()
        self.dot.setFixedSize(7, 7)
        restyle(self.dot, lambda: f"background: {theme.tok('acc')}; border-radius: 3px;")
        self.dot.hide()
        trow.addWidget(self.dot)
        trow.addStretch(1)
        tcol.addLayout(trow)
        self.ctx = label("", 12, "t4", wrap=False)
        tcol.addWidget(self.ctx)
        head.addLayout(tcol, 1)
        col.addLayout(head)
        col.addSpacing(8)
        self.msgs = QVBoxLayout()
        self.msgs.setSpacing(16)
        self.msgs.addStretch(1)
        col.addLayout(self.msgs, 1)
        self.sample = []
        if sample_chat:
            me = label("这一章为什么先从一场公开的酷刑讲起？", 14, "t1")
            me.setObjectName("me")
            restyle(me, lambda: f"QLabel#me {{ background: {theme.tok('bg4')}; color: {theme.tok('t1')};"
                                f" border-radius: 12px; padding: 9px 13px; }}")
            bot = label("（这是演示用的替身栏，不是真的 AI 回答。）真的左栏由 C 做：回答流式显示，"
                        "引用原文用「」括起来。", 14, "t2")
            me.setMinimumWidth(200)
            self.msgs.addWidget(me)
            self.msgs.addWidget(bot)
            self.sample = [me, bot]
        self.card = QFrame(self)
        self.card.setObjectName("quiz")
        self.card.hide()
        self.msgs.addWidget(self.card)
        col.addSpacing(12)
        self.composer = QFrame(self)
        self.composer.setObjectName("composer")
        restyle(self.composer, lambda: f"QFrame#composer {{ background: {theme.tok('bg0')};"
                                       f" border: 1px solid {theme.tok('l2')}; border-radius: 12px; }}")
        cl = QVBoxLayout(self.composer)
        cl.setContentsMargins(12, 10, 12, 8)
        self.ph = label("问这本书的任何事…", 14, "t5")
        self.ph.setMinimumHeight(42)
        cl.addWidget(self.ph)
        kb = QHBoxLayout()
        kb.addWidget(label("Ctrl+Enter", 11, "t4", wrap=False))
        kb.addStretch(1)
        cl.addLayout(kb)
        col.addWidget(self.composer)

    # 接口
    def set_companion(self, companion):
        self.calls.append(("set_companion", companion is not None))
        self.companion = companion
        if companion is None:
            self.ph.setText("AI 还没登录：去书架登录 reader 专用的 Claude 账号后才能问")

    def set_context(self, loc, text):
        self.calls.append(("set_context", loc, text))
        self.ctx.setText(f"看得到：{text}"[:34])

    def attach_quote(self, text, loc):
        self.calls.append(("attach_quote", text, loc))

    def show_quiz(self, quiz):
        self.calls.append(("show_quiz", quiz.section))
        self.quiz = quiz
        self._pick = None
        self.dot.show()
        self.ph.setText("答完这题再接着问")
        self.composer.setEnabled(False)
        for w in self.sample:
            w.hide()
        self._build_card()

    def quiz_timeout_note(self):
        self.calls.append(("quiz_timeout_note",))
        if self.quiz is not None:
            self._foot.setText("时间到了，这题答不答都行")

    def stop(self):
        self.calls.append(("stop",))

    # 替身自己的小测卡
    def _build_card(self):
        old = self.card.layout()
        if old is not None:
            QWidget().setLayout(old)
        q = self.quiz
        restyle(self.card, lambda: f"QFrame#quiz {{ background: {theme.tok('bg3')}; border: 1px solid "
                                   f"{rgba('acc', 0.6)}; border-radius: 12px; }}")
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(14, 13, 14, 12)
        lay.setSpacing(6)
        lay.addWidget(label(f"小测 · {self._sec_title(q.section)}", 12, "acc", 600, wrap=False))
        lay.addWidget(label(q.question, 15, "t1", 500))
        self._opts = []
        for i, o in enumerate(q.options):
            b = QPushButton(f"{'ABCD'[i]}　{o}")
            no_focus(b)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=i: self.pick(k))
            b.setStyleSheet(f"QPushButton {{ text-align: left; height: auto; padding: 8px 10px;"
                            f" border: 1px solid {theme.tok('l2')}; border-radius: 9px; background: transparent;"
                            f" color: {theme.tok('t2')}; font-size: 13px; }}")
            lay.addWidget(b)
            self._opts.append(b)
        self._verdict = label("", 13, "t2")
        lay.addWidget(self._verdict)
        foot = QHBoxLayout()
        self._foot = label("答完再读下一节 · 可以往回翻书", 12, "t4", wrap=False)
        foot.addWidget(self._foot)
        foot.addStretch(1)
        self._go = button("就选…", "pri", height=28)
        self._go.setEnabled(False)
        self._go.clicked.connect(self.confirm)
        foot.addWidget(self._go)
        lay.addLayout(foot)
        self.card.show()

    def _sec_title(self, idx):
        c = self.companion
        try:
            return c.book.sections[idx].title[:16]
        except Exception:  # noqa: BLE001
            return f"第 {idx + 1} 节"

    def pick(self, i):
        self.activity.emit()
        self._pick = i
        self._go.setText(f"就选 {'ABCD'[i]}")
        self._go.setEnabled(True)

    def confirm(self):
        from ..ai.companion import Companion
        if self.quiz is None or self._pick is None or self.quiz.finished:
            return
        self.activity.emit()
        g = Companion.grade(self.quiz, self._pick)
        self._verdict.setText(("答对了。" if g.correct else "不太对。") + g.explanation[:80])
        if not g.correct:
            self.jump_to.emit(g.quote_loc, g.quote)
        if self.quiz.finished:
            idx, ok = self.quiz.section, g.correct
            self.quiz = None
            self.dot.hide()
            self.composer.setEnabled(True)
            self.ph.setText("问这本书的任何事…")
            self.quiz_finished.emit(idx, ok)


# ── 演示用的假 AI（不花额度） ──────────────────────────────
def demo_companion(book, delay: float = 2.0):
    """用书里真的句子拼一道「哪句是原文」的题。只为演示流程，不是真的出题。"""
    from ..ai.claude import Result
    from ..ai.companion import Companion

    def ask_once(prompt, *, system, timeout=240, stop=None):
        time.sleep(delay)
        body = prompt.split("【原文】", 1)[-1]
        sents = [s.strip() for s in re.split(r"[。！？]", body) if 12 <= len(s.strip()) <= 60]
        if len(sents) < 4:
            return Result(error="这一节太短，演示不出题")
        rnd = random.Random(len(body))
        pick = rnd.sample(sents, 4)
        answer = rnd.randrange(4)
        opts = [s[:26] + ("…" if len(s) > 26 else "") for s in pick]
        data = {"question": "（演示题）下面哪一句出现在这一节靠前的地方？", "options": opts,
                "answer": answer, "hints": ["再回原文找找看。"] * 4, "explain": "演示题，只看流程。",
                "quote": pick[answer]}
        return Result(text=json.dumps(data, ensure_ascii=False))

    def run(prompt, *, system=None, resume=None, stop=None, **kw):
        yield Result(text="（演示）", session_id="demo")

    return Companion(book, runner=run, ask_once=ask_once)


class SpeedClock:
    """演示用：时间可以快进（speed 倍）。"""

    def __init__(self, speed: float = 1.0):
        self.t0 = time.time()
        self.speed = speed
        self.offset = 0.0

    def __call__(self) -> float:
        return self.t0 + (time.time() - self.t0) * self.speed + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def find_demo_book() -> str | None:
    dl = Path.home() / "Downloads"
    for pat in ("*.epub", "*.pdf"):
        hits = sorted(dl.glob(pat))
        if hits:
            return str(hits[0])
    return None


def demo_deps(*, clock, state_path, status_path, idle_timeout=300, real_parts: bool = True):
    """演示的依赖：能导入 B、C 的真零件就用真的，不然用替身；AI 用假的；不进锁屏。"""
    from .reading import ReadingDeps
    make_view = lambda book: FakeBookView()      # noqa: E731
    make_panel = FakeAiPanel
    if real_parts:
        try:
            from .reading import _default_view
            from .bookview import epub_view  # noqa: F401
            make_view = _default_view
        except ImportError:
            pass
        try:
            from .ai_panel import AiPanel  # noqa: F401
            from .reading import _default_panel
            make_panel = _default_panel
        except ImportError:
            pass
    from ..lock.windows import LeaveWatcher
    return ReadingDeps(make_view=make_view, make_panel=make_panel, make_companion=demo_companion,
                       logged_in=lambda: True, clock=clock, state_path=state_path,
                       status_path=status_path, idle_timeout=idle_timeout,
                       make_watcher=LeaveWatcher)
