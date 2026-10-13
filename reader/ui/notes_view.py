"""读完之后的总结 + 读书笔记（界面稿板 10②）。书架上点一篇旧笔记也是这一页。

左边 360px：这次读了多久（发呆扣了几分钟）、页数、小测、问了几次、划线、今天够没够，底下「回书架」。
右边：读书笔记像一页纸（Markdown 渲染）。最底下一行写存在哪 +「打开文件夹」。
笔记没写成：纸上说清楚「问答和答题记录已经存了」，有办法重写就给「重新写一次」。

这次的数字来自 ReadingScreen.finished 的总结 dict（D 写的），键：
minutes, idle_deducted, pages, quizzes, asked, highlights, note_path, reason,
note_pending + rewrite_note（笔记还没写：rewrite_note() 在工作线程里写好、存好，返回路径）。
每个键都可能缺、格式也可能不一样（pages 可以是 [起, 止] / 页数 / 一句话；quizzes 可以是列表 / dict / 数字），
这里都认，认不出来就不显示那一行，不报错。

## 笔记还在写（板 10②）
读完马上就到这一页，不等 Claude：note_pending 且给了 rewrite_note 时，纸上先写「Claude 在写读书笔记…」，
后台调 rewrite_note()，写好了纸上换成笔记；写不成就换成「没写成」+「重新写一次」。
中途主人回了书架或者点开了别的笔记，晚到的结果不会盖掉正在看的那一页。
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument,
                           QTextFormat, QTextFrameFormat)
from PySide6.QtWidgets import (QHBoxLayout, QTextBrowser, QVBoxLayout, QWidget)

from reader.ui import theme
from reader.ui.shelf_kit import (Button, Card, IconLabel, Label, PBar, background, cap, hline, lbl,
                                 md, on_theme, part_of_day, row)

NOTE_PX = 15
NOTE_LINE = 1.85


# ── 总结里各项的说法 ──────────────────────────────────────────
def _num(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fmt_pages(v) -> tuple[str, str] | None:
    """(值, 旁边的小字)：[132, 151] → ("132 → 151", "19 页")。"""
    if v is None or v == "":
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        a, b = v
        na, nb = _num(re.sub(r"\D", "", str(a)) or None), _num(re.sub(r"\D", "", str(b)) or None)
        text = f"{na if na is not None else a} → {nb if nb is not None else b}"
        extra = f"{nb - na} 页" if na is not None and nb is not None and nb > na else ""
        return text, extra
    if isinstance(v, dict):
        return fmt_pages([v.get("from") or v.get("start"), v.get("to") or v.get("end")])
    if isinstance(v, (int, float)):
        return f"{int(v)} 页", ""
    return str(v), ""


def _quiz_list(v) -> list[dict]:
    out = []
    for q in v:
        if isinstance(q, dict):
            attempts = q.get("attempts") or []
            ok = q.get("correct")
            if ok is None:
                ok = any(a.get("correct") for a in attempts if isinstance(a, dict))
            out.append({"ok": bool(ok), "tries": len(attempts) or (1 if ok else 2)})
        elif hasattr(q, "attempts"):
            att = list(getattr(q, "attempts") or [])
            ok = any(a.get("correct") for a in att)
            out.append({"ok": ok, "tries": len(att)})
    return out


def fmt_quizzes(v) -> tuple[str, str] | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return (f"{int(v)} 题", "") if v else ("这次没出题", "")
    if isinstance(v, dict):
        total, right = _num(v.get("total")), _num(v.get("correct"))
        second = _num(v.get("second")) or 0
        if total is None:
            return None
        items = [{"ok": i < (right or 0), "tries": 2 if i < second else 1} for i in range(total)]
    elif isinstance(v, (list, tuple)):
        items = _quiz_list(v)
    else:
        return str(v), ""
    if not items:
        return "这次没出题", ""
    total = len(items)
    right = sum(1 for q in items if q["ok"])
    second = sum(1 for q in items if q["ok"] and q["tries"] >= 2)
    main = f"{total} 题全对" if right == total else f"{total} 题对了 {right} 题"
    return main, (f"{second} 题是第二次" if second else "")


def fmt_count(v, unit: str) -> tuple[str, str] | None:
    if isinstance(v, (list, tuple)):
        v = len(v)
    n = _num(v)
    if n is None:
        return None
    return (f"{n} {unit}" if n else "没有"), ""


def page_range(v) -> str:
    """笔记页眉里的「第 132–151 页」。"""
    if isinstance(v, (list, tuple)) and len(v) == 2:
        a, b = (re.sub(r"\D", "", str(x)) for x in v)
        if a and b:
            return f"第 {a}–{b} 页" if a != b else f"第 {a} 页"
    return ""


def split_title(markdown: str) -> tuple[str, str]:
    """(第一行 # 标题, 去掉标题后的正文)。"""
    lines = markdown.lstrip("﻿").splitlines()
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        if ln.startswith("# "):
            return ln[2:].strip(), "\n".join(lines[i + 1:]).strip()
        break
    return "", markdown.strip()


def open_folder(path: Path) -> None:
    """在资源管理器里打开，并选中这个文件。"""
    path = Path(path)
    if os.name == "nt":
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            os.startfile(str(path if path.is_dir() else path.parent))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


# ── 笔记那张纸 ────────────────────────────────────────────────
class NotePaper(QTextBrowser):
    """一页纸：页眉（读书笔记 / 标题 / 日期页数分钟）+ Markdown 正文。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QTextBrowser.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._args: tuple | None = None
        on_theme(self, self._rerender)
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {theme.tok('ink')}; border: 0; "
                           f"selection-background-color: {theme.tok('bg5')}; }}")

    def _rerender(self) -> None:
        self._restyle()
        if self._args:
            self.show_note(*self._args)

    def show_note(self, title: str, meta: str, body_md: str, kicker: str = "读书笔记") -> None:
        self._args = (title, meta, body_md, kicker)
        doc = QTextDocument(self)
        doc.setDefaultFont(theme.font(NOTE_PX, 400))
        doc.setIndentWidth(18)
        doc.setMarkdown(body_md, QTextDocument.MarkdownDialectGitHub)
        root = doc.rootFrame().frameFormat()
        root.setLeftMargin(44)
        root.setRightMargin(44)
        root.setTopMargin(34)
        root.setBottomMargin(40)
        doc.rootFrame().setFrameFormat(root)
        self._format_body(doc)
        self._insert_header(doc, kicker, title, meta)
        self.setDocument(doc)

    def _format_body(self, doc: QTextDocument) -> None:
        ink = theme.color("ink")
        lh = NOTE_LINE / theme.MISANS_NATURAL * 100
        b = doc.begin()
        while b.isValid():
            c = QTextCursor(b)
            bf = b.blockFormat()
            level = bf.headingLevel()
            bf.setLineHeight(lh if not level else 140, QTextBlockFormat.ProportionalHeight.value)
            if level:
                bf.setTopMargin(20)
                bf.setBottomMargin(4)
            else:
                bf.setTopMargin(0)
                bf.setBottomMargin(2 if b.textList() else 8)
            c.setBlockFormat(bf)
            c.movePosition(QTextCursor.StartOfBlock)
            c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            cf = QTextCharFormat()
            cf.setForeground(ink)
            cf.setFontFamilies([theme.family()])
            if level:
                cf.setProperty(QTextFormat.FontSizeAdjustment, 0)
                cf.setProperty(QTextFormat.FontPixelSize, 14 if level > 1 else 18)
                cf.setFontWeight(600)
            c.mergeCharFormat(cf)
            b = b.next()

    def _insert_header(self, doc: QTextDocument, kicker: str, title: str, meta: str) -> None:
        c = QTextCursor(doc)
        c.movePosition(QTextCursor.Start)
        for _ in range(3):
            c.insertBlock()
        specs = [
            (kicker, 12, 400, "ink2", 1.2, 0, 4),
            (title, 22, 600, "ink", 0, 0, 2),
            (meta, 12, 400, "ink2", 0, 0, 6),
        ]
        for i, (text, px, w, color, spacing, top, bottom) in enumerate(specs):
            blk = doc.findBlockByNumber(i)
            if blk.textList():
                blk.textList().remove(blk)
            cur = QTextCursor(blk)
            bf = QTextBlockFormat()
            bf.setTopMargin(top)
            bf.setBottomMargin(bottom)
            bf.setLineHeight(140, QTextBlockFormat.ProportionalHeight.value)
            cur.setBlockFormat(bf)
            cf = QTextCharFormat()
            f = theme.font(px, w, tnum=True)
            if spacing:
                f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
            cf.setFont(f)
            cf.setForeground(theme.color(color))
            cur.setBlockCharFormat(cf)
            cur.insertText(text, cf)


# ── 整页 ────────────────────────────────────────────────────
class NotesView(QWidget):
    """back()：点了「回书架」。"""

    back = Signal()

    def __init__(self, library, *, today_fn=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.library = library
        self.today_fn = today_fn or (lambda: (0, 45))     # () -> (今天分钟, 目标)
        self.note_path: Path | None = None
        self._rewrite = None
        self._gen = 0                 # 每显示一次加一；后台写好的笔记只填回同一次显示
        self._meta = ""
        self._book = ""
        self.writing = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        outer.addLayout(mid, 1)

        # 左栏
        self.left = Card("bg1", None, 0)
        self.left.setFixedWidth(360)
        self._left_lay = QVBoxLayout(self.left)
        self._left_lay.setContentsMargins(28, 30, 28, 24)
        self._left_lay.setSpacing(0)
        mid.addWidget(self.left)
        self.vline = Card("l1", None, 0)
        self.vline.setFixedWidth(1)
        mid.addWidget(self.vline)

        # 右边：桌面 + 纸
        desk = Card("desk", None, 0)
        dl = QHBoxLayout(desk)
        dl.setContentsMargins(40, 28, 40, 0)
        dl.addStretch(1)
        self.paper_card = Card("paper", None, 6, radii=(6, 6, 0, 0), shadow=(4, 20))
        self.paper_card.setFixedWidth(640)
        pl = QVBoxLayout(self.paper_card)
        pl.setContentsMargins(0, 0, 0, 0)
        self.paper = NotePaper()
        pl.addWidget(self.paper)
        # 笔记没写成时，纸上换成这一块：说清楚丢没丢东西 +「重新写一次」
        self.fail_box = QWidget()
        fb = QVBoxLayout(self.fail_box)
        fb.setContentsMargins(44, 34, 44, 34)
        fb.setSpacing(0)
        fb.addWidget(Label("读书笔记", 12, 400, "ink2", spacing=1.2))
        fb.addSpacing(4)
        self.fail_title = Label("", 22, 600, "ink", wrap=True)
        fb.addWidget(self.fail_title)
        fb.addSpacing(18)
        self.fail_msg = Label("", 15, 400, "ink", wrap=True)
        fb.addWidget(self.fail_msg)
        fb.addSpacing(18)
        self.retry_btn = Button("重新写一次", "pri")
        self.retry_btn.clicked.connect(self._do_rewrite)
        self.retry_msg = Label("", 13, 400, "ink2", wrap=True)
        fb.addLayout(row(self.retry_btn, self.retry_msg, None, spacing=12))
        fb.addStretch(1)
        self.fail_box.setVisible(False)
        pl.addWidget(self.fail_box)
        # 笔记还在写：纸上照样有页眉，正文位置写「Claude 在写读书笔记…」
        self.wait_box = QWidget()
        wb = QVBoxLayout(self.wait_box)
        wb.setContentsMargins(44, 34, 44, 34)
        wb.setSpacing(0)
        wb.addWidget(Label("读书笔记", 12, 400, "ink2", spacing=1.2))
        wb.addSpacing(4)
        self.wait_title = Label("", 22, 600, "ink", wrap=True)
        wb.addWidget(self.wait_title)
        wb.addSpacing(2)
        self.wait_meta = Label("", 12, 400, "ink2", wrap=True)
        wb.addWidget(self.wait_meta)
        wb.addSpacing(26)
        self.wait_msg = Label("AI 在写读书笔记…", 15, 400, "ink2", wrap=True)
        wb.addWidget(self.wait_msg)
        wb.addSpacing(6)
        wb.addWidget(Label("这次读的原文、问答和答题记录都交给它了，一般半分钟左右。可以先看左边的总结。",
                           13, 400, "ink2", wrap=True))
        wb.addStretch(1)
        self.wait_box.setVisible(False)
        pl.addWidget(self.wait_box)
        dl.addWidget(self.paper_card)
        dl.addStretch(1)
        mid.addWidget(desk, 1)

        # 底栏：存在哪 + 打开文件夹
        foot = Card("bg1", None, 0)
        foot.setFixedHeight(44)
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        fl.addWidget(hline("l1"))
        fr = QHBoxLayout()
        fr.setContentsMargins(388, 0, 20, 0)
        self.where = cap("")
        self.open_btn = Button("打开文件夹", "ghost", "sm", icon_name="folder")
        self.open_btn.clicked.connect(self._open_folder)
        fr.addWidget(self.where, 1)
        fr.addWidget(self.open_btn)
        fl.addLayout(fr, 1)
        outer.addWidget(foot)

    # ── 对外 ────────────────────────────────────────────
    def show_session(self, entry, summary: dict, started: datetime | None = None,
                     ended: datetime | None = None, *, fresh: bool = True, book_title: str = "") -> None:
        """刚读完（fresh）或从书架点开一次记录。entry 是 BookEntry（可以是 None）。"""
        summary = dict(summary or {})
        self._gen += 1
        self.writing = False
        title_book = entry.title if entry is not None else book_title
        self._book = title_book
        self._rewrite = summary.get("rewrite_note") if callable(summary.get("rewrite_note")) else None
        self._build_left(summary, started, ended, fresh)
        note = summary.get("note_path")
        self.note_path = Path(note) if note else None
        meta_bits = []
        if started:
            meta_bits.append(f"{md(started.date())}{part_of_day(started)}")
        pr = page_range(summary.get("pages"))
        if pr:
            meta_bits.append(pr)
        if _num(summary.get("minutes")) is not None:
            meta_bits.append(f"{_num(summary.get('minutes'))} 分钟")
        self._meta = " · ".join(meta_bits)
        if not self.note_path and summary.get("note_pending") and self._rewrite is not None:
            self._start_writing(title_book)
        elif self.note_path and self.note_path.is_file():
            try:
                text = self.note_path.read_text("utf-8")
            except OSError as e:
                self._show_missing(title_book, f"这篇笔记读不出来：{e}")
            else:
                h1, body = split_title(text)
                meta_bits.append("AI 写的")
                self._show_paper(h1 or f"《{title_book}》", " · ".join(meta_bits), body)
        elif self.note_path:
            self._show_missing(title_book, "笔记文件不见了（可能被挪走或删掉了）。")
        else:
            err = summary.get("note_error")
            skipped = summary.get("note_skipped")
            if skipped:
                self._show_missing(title_book, f"{skipped}。想要的话点下面的按钮让 AI 写一篇。")
                self.retry_btn.setText("写一篇")
            else:
                self._show_missing(title_book, f"{err}。问答和答题记录已经存了，不会丢。" if err else "")
        self._set_where()

    def show_note_file(self, path: Path) -> None:
        """书架上点一篇笔记：有这次读书的记录就连总结一起显示，没有就只显示笔记。"""
        rec = self.library.session_for_note(path) if self.library is not None else None
        if rec:
            entry = self.library.get(rec.get("book_id", ""))
            self.show_session(entry, rec.get("summary") or {}, _dt(rec.get("started")), _dt(rec.get("ended")),
                              fresh=False)
            return
        mtime = datetime.fromtimestamp(Path(path).stat().st_mtime) if Path(path).exists() else None
        self.show_session(None, {"note_path": str(path)}, mtime, None, fresh=False,
                          book_title=Path(path).parent.name)

    # ── 左栏 ────────────────────────────────────────────
    def _clear_left(self) -> None:
        lay = self._left_lay
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
            elif it.layout() is not None:
                _drop_layout(it.layout())

    def _build_left(self, s: dict, started: datetime | None, ended: datetime | None, fresh: bool) -> None:
        self._clear_left()
        lay = self._left_lay
        span = f"{started:%H:%M}–{ended:%H:%M}" if started and ended else ""
        if fresh:
            head = "这次提前结束了" if s.get("reason") == "emergency_exit" else "这次读完了"
        else:
            head = md(started.date()) if started else "读书笔记"
        lay.addWidget(lbl(f"{head} · {span}" if span else head))

        minutes = _num(s.get("minutes"))
        if minutes is not None:
            lay.addSpacing(12)
            big = QHBoxLayout()
            big.setSpacing(6)
            n = Label(str(minutes), 40, 600, "t1", tnum=True)
            unit = Label("分钟", 16, 400, "t4")
            big.addWidget(n, 0, Qt.AlignBottom)
            big.addWidget(unit, 0, Qt.AlignBottom)
            big.addStretch(1)
            lay.addLayout(big)
            idle = _num(s.get("idle_deducted"))
            sub = f"发呆扣掉了 {idle} 分钟" if idle else "没有扣时间"
            if s.get("reason") == "emergency_exit":
                sub = "走了紧急出口，读过的照算" + (f" · 发呆扣掉了 {idle} 分钟" if idle else "")
            lay.addSpacing(6)
            lay.addWidget(cap(sub))

        stats = [("页数", fmt_pages(s.get("pages"))), ("小测", fmt_quizzes(s.get("quizzes"))),
                 ("问 AI", fmt_count(s.get("asked"), "次")), ("划线", fmt_count(s.get("highlights"), "处"))]
        stats = [(k, v) for k, v in stats if v]
        if stats:
            lay.addSpacing(18)
        for k, (val, extra) in stats:
            r = QHBoxLayout()
            r.setContentsMargins(0, 9, 0, 9)
            r.addWidget(Label(k, 13, 400, "t4"))
            r.addStretch(1)
            r.addWidget(Label(val, 13, 400, "t1", tnum=True))
            if extra:
                r.addSpacing(6)
                r.addWidget(cap(extra))
            lay.addLayout(r)
            lay.addWidget(hline("l1"))

        if fresh or (started and started.date() == datetime.now().date()):
            today, target = self.today_fn()
            lay.addSpacing(24)
            lay.addWidget(self._today_box(today, target))

        lay.addStretch(1)
        back = Button("回书架", "pri")
        back.clicked.connect(self.back.emit)
        lay.addLayout(row(None, back))

    def _today_box(self, today: int, target: int) -> QWidget:
        box = Card("bg3", "l1", 10)
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 14, 14, 14)
        v.setSpacing(0)
        full = today >= target
        title = "今天读满了" if full else f"今天还差 {target - today} 分钟"
        right = QHBoxLayout()
        right.setSpacing(4)
        if full:
            right.addWidget(IconLabel("ok", "greent", 14))
        right.addWidget(Label(f"{today} / {target}", 13, 400, "greent" if full else "t2", tnum=True))
        v.addLayout(row(Label(title, 14, 600, "t1"), None, right))
        v.addSpacing(10)
        v.addWidget(PBar(min(1.0, today / target) if target else 0, "green" if full else "acc", "bg5"))
        parts = []
        for rec in self.library.sessions(datetime.now().strftime("%Y-%m-%d")) if self.library else []:
            m = _num((rec.get("summary") or {}).get("minutes"))
            st = _dt(rec.get("started"))
            if m and st:                       # 0 分钟的（刚开始就走了）不列
                parts.append(f"{part_of_day(st)} {m} 分钟")
        line = " + ".join(parts) if len(parts) > 1 else ""
        if line:
            v.addSpacing(8)
            v.addWidget(cap(line, wrap=True))
        return box

    # ── 笔记没写成 ──────────────────────────────────────
    def _show_paper(self, title: str, meta: str, body: str) -> None:
        self.fail_box.setVisible(False)
        self.wait_box.setVisible(False)
        self.paper.setVisible(True)
        self.paper.show_note(title, meta, body)

    def _start_writing(self, book: str) -> None:
        """纸上写「Claude 在写读书笔记…」，后台调 rewrite_note()。"""
        self.writing = True
        self.paper.setVisible(False)
        self.fail_box.setVisible(False)
        self.wait_box.setVisible(True)
        self.wait_title.setText(f"《{book}》" if book else "")
        self.wait_meta.setText(self._meta)
        gen, fn = self._gen, self._rewrite

        def done(res, err):
            if gen != self._gen:          # 主人已经在看别的了
                return
            self.writing = False
            if err is not None or not res:
                self._show_missing(book, f"读书笔记没写成：{err or '没有返回'}。问答和答题记录已经存了，不会丢。")
                self._set_where()
                return
            self._show_written(Path(res))
        background(fn, done)

    def _show_written(self, path: Path) -> None:
        self.note_path = Path(path)
        try:
            h1, body = split_title(self.note_path.read_text("utf-8"))
        except OSError as e:
            self._show_missing(self._book, f"这篇笔记读不出来：{e}")
            return
        meta = " · ".join(b for b in (self._meta, "AI 写的") if b)
        self._show_paper(h1 or (f"《{self._book}》" if self._book else "读书笔记"), meta, body)
        self._set_where()

    def _show_missing(self, book: str, why: str) -> None:
        msg = why or "这次的读书笔记没写成。问答和答题记录已经存了，不会丢。"
        self.paper.setVisible(False)
        self.wait_box.setVisible(False)
        self.fail_box.setVisible(True)
        self.fail_title.setText(f"《{book}》" if book else "")
        self.fail_msg.setText(msg)
        self.retry_btn.setVisible(self._rewrite is not None)
        self.retry_btn.setText("重新写一次")    # 「写一篇」只用在读太短没自动写的那种
        self.retry_msg.setText("")
        self.retry_btn.setEnabled(True)

    def _do_rewrite(self) -> None:
        fn = self._rewrite
        if fn is None:
            return
        self.retry_btn.setEnabled(False)
        self.retry_msg.setText("AI 在写读书笔记…")

        gen = self._gen

        def done(res, err):
            if gen != self._gen:
                return
            if err is not None or not res:
                self.retry_btn.setEnabled(True)
                self.retry_msg.setText(f"还是没写成：{err or '没有返回'}。可以再试一次。")
                return
            self._show_written(Path(res))
        background(fn, done)

    # ── 底栏 ────────────────────────────────────────────
    def _set_where(self) -> None:
        p = self.note_path
        if self.writing:
            self.where.setText(f"写好了存进「{self.library.notes_root.name if self.library else '笔记'}」文件夹")
            self.where.setToolTip("")
            self.open_btn.setEnabled(False)
        elif p and p.exists():
            try:
                shown = p.relative_to(self.library.root) if self.library else p
            except ValueError:
                shown = p
            self.where.setText(f"存在 {shown}")
            self.where.setToolTip(str(p))
            self.open_btn.setEnabled(True)
        else:
            self.where.setText("这次没有存下笔记文件")
            self.where.setToolTip("")
            self.open_btn.setEnabled(False)

    def _open_folder(self) -> None:
        if self.note_path:
            open_folder(self.note_path)


def _dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def _drop_layout(lay) -> None:
    while lay.count():
        it = lay.takeAt(0)
        if it.widget() is not None:
            it.widget().deleteLater()
        elif it.layout() is not None:
            _drop_layout(it.layout())


if __name__ == "__main__":
    # 演示：在临时书架里存一篇示例笔记 + 一次读书记录，打开这一页。--fail 看「没写成」。不会锁屏。
    import sys
    import tempfile

    from PySide6.QtWidgets import QApplication

    from reader.library import Library

    theme.prepare_platform()
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    lib = Library(Path(tempfile.mkdtemp(prefix="reader-notes-")))
    dl = Path.home() / "Downloads"
    entry = lib.add(next(iter(dl.glob("*.epub")), None) or next(iter(dl.glob("*.pdf"))))
    now = datetime.now().replace(second=0, microsecond=0)
    note = None if "--fail" in sys.argv else lib.save_note(entry.id, (
        f"# 《{entry.title}》· {now:%Y-%m-%d}\n\n## 这次读了什么\n\n"
        "- **酷刑的消失**：惩罚从公开的肉体痛苦，变成看不见的剥夺。\n"
        "- **公开处决的仪式**：处决是一场权力的展示，观众既是见证人也是参与者。\n\n"
        "## 我问过的问题\n\n- 为什么说监狱是「更温和」的？—— 不是更温和，是换了作用的对象：从身体到灵魂。\n\n"
        "## 小测\n\n- 2 题全对。\n\n## 值得记住的原文\n\n- 「惩罚逐渐不再是一种公开表演。」\n\n"
        "## 留给下次\n\n- 纪律是怎样从军队、学校扩散到整个社会的？\n"), now)
    summary = {"minutes": 27, "idle_deducted": 2, "pages": [132, 151],
               "quizzes": [{"attempts": [{"correct": True}]}, {"attempts": [{"correct": False}, {"correct": True}]}],
               "asked": 4, "highlights": 3, "note_path": str(note) if note else None, "reason": "done"}
    if "--fail" in sys.argv:
        summary["rewrite_note"] = lambda: (_ for _ in ()).throw(RuntimeError("演示：还是写不成"))
    v = NotesView(lib, today_fn=lambda: (45, 45))
    v.resize(1280, 800)
    v.back.connect(app.quit)
    v.show_session(entry, summary, now.replace(hour=14, minute=5), now.replace(hour=14, minute=34))
    v.show()
    theme.titlebar(v)
    sys.exit(app.exec())
