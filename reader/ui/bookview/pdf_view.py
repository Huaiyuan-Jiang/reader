"""PDF 书页：PyMuPDF 按设备像素出图，默认适合宽度、一页接一页往下滚（界面稿板 3）。

## 出图
- 只出看得见的页（再往前后各预备一两页），放进按内存算的缓存（最近用过的留着）。
- 出图在子进程里（见 `_render.py`：PyMuPDF 出图时占着 Python 的锁，放线程里界面会顿）。
  图回来之前先画一张纸色的空页；换大小时先把旧图缩放着顶上。
- 图按设备像素出、贴的时候对齐到设备像素，不再缩放 —— 跟 QtPdf 一样清楚。
- 暗色主题：默认「调暗的暖纸」（白 → `pdfpaper`、黑 → `pdfink`，中间按比例）；Aa 里能选反色（深底浅字，
  颜色跟 EPUB 书页一样）或原样。亮色主题照原样（白换成 `pdfpaper` 那一点点暖）。

## 选字、划线
- 字的位置来自 `book.page_words(p)`（文字层或者本机文字识别的结果，PDF 坐标）。
  一个「词」里有汉字就按字平分成一个个字，中文能一个字一个字地选；英文按词选。
- 这一页没有字（扫描版还没识别到）就不能选；底栏写明。
- 高亮存 `{"page": p, "rects": [[x0, y0, x1, y1], ...], "text": ...}`（PDF 坐标，一行一个框）。

## 找出处
先用 `page.search_for`（有文字层时），找不到（跨行的中文、扫描版）就在 page_words 上按字比对（先精确、再按相似度），
这一页没有就看后一页、前一页；都不行至少跳到那一页。

## 双页（Aa 里选）
第 1、2 页并排，第 3、4 页并排……一排两页顶端对齐，还是一排接一排往下滚。「整页」+「双页」就是摊开的书。
窗口不够宽（一页不到 SPREAD_MIN_W）先单页。挡板所在那一排，过了挡板的那一页不画、点不到。
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import re
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QLabel, QScrollBar, QWidget

from reader.books import Book, Loc
from reader.ui import theme

from . import _render
from .base import AaPanel, BookView, Seg, find_in, load_prefs, norm_chars, save_prefs

GAP = 14            # 页和页之间
PAD_Y = 22          # 界面稿 .pdfp .page padding-top
PAD_X = 40
SPREAD_GAP = 24     # 双页时左右两页中间空多宽
SPREAD_MIN_W = 320  # 双页时一页至少这么宽，窗口不够宽就先单页
CACHE_BYTES = 160 * 1024 * 1024   # DPR 1.5、适合宽度时一页约 8MB：够放二十来页
_CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")


# ── 出图的后台 ────────────────────────────────────────────────
class _QConn:
    """线程版的连接替身（子进程起不来时用）。"""

    def __init__(self, q_in: queue.Queue, q_out: queue.Queue):
        self.q_in, self.q_out = q_in, q_out

    def recv(self):
        return self.q_in.get()

    def poll(self):
        return not self.q_in.empty()

    def send(self, msg):
        self.q_out.put(msg)


class Renderer(QObject):
    """一本书一个。`want(jobs)` 说现在要哪些页；图出来了发 `ready(页, 宽像素, 颜色, QImage)`。"""

    ready = Signal(int, int, object, QImage)
    failed = Signal(int, str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._last: list | None = None
        self.mode = "process"
        self._closed = False
        try:
            if os.environ.get("READER_PDF_RENDER") == "thread":
                raise RuntimeError("按设置用线程")
            ctx = multiprocessing.get_context("spawn")
            req_r, self._req = ctx.Pipe(duplex=False)
            self._res, res_w = ctx.Pipe(duplex=False)
            self._proc = ctx.Process(target=_render.worker, args=(path, req_r, res_w), daemon=True,
                                     name="reader-pdf-render")
            self._proc.start()
            req_r.close()
            res_w.close()
        except Exception:  # noqa: BLE001 —— 子进程不行就用线程
            self.mode = "thread"
            self._proc = None
            a, b = queue.Queue(), queue.Queue()
            self._req = _QConn(None, a)
            self._res = _QConn(b, None)
            self._worker = threading.Thread(target=_render.worker, args=(path, _QConn(a, None), _QConn(None, b)),
                                            daemon=True, name="reader-pdf-render")
            self._worker.start()
        self._pump = threading.Thread(target=self._pump_loop, daemon=True, name="reader-pdf-pump")
        self._pump.start()

    def _pump_loop(self) -> None:
        while True:
            try:
                msg = self._res.recv()
            except (EOFError, OSError):
                return
            if self._closed:
                return
            if msg[0] == "img":
                _, page, px_w, colors, w, h, data = msg
                img = QImage(data, w, h, 4 * w, QImage.Format_RGB32).copy()
                try:
                    self.ready.emit(page, px_w, colors, img)
                except RuntimeError:
                    return
            elif msg[0] == "err":
                try:
                    self.failed.emit(msg[1], msg[4])
                except RuntimeError:
                    return

    def want(self, jobs: list) -> None:
        if jobs == self._last or self._closed:
            return
        self._last = list(jobs)
        try:
            self._req.send(("want", jobs))
        except (OSError, EOFError, ValueError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._req.send(("quit",))
        except (OSError, EOFError, ValueError):
            pass
        if self._proc is not None:
            self._proc.join(0.5)
            if self._proc.is_alive():
                self._proc.terminate()


# ── 字的位置 ─────────────────────────────────────────────────
@dataclass
class _Unit:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: int
    cjk: bool


def page_units(words) -> list[_Unit]:
    """page_words → 能选的最小单位：汉字一个字一个，英文一个词一个。"""
    out = []
    for x0, y0, x1, y1, w, line in words:
        w = str(w)
        if not w.strip():
            continue
        if len(w) > 1 and _CJK.search(w):
            step = (x1 - x0) / len(w)
            for i, c in enumerate(w):
                out.append(_Unit(c, x0 + i * step, y0, x0 + (i + 1) * step, y1, int(line), bool(_CJK.match(c))))
        else:
            out.append(_Unit(w, x0, y0, x1, y1, int(line), bool(_CJK.search(w))))
    return out


def units_text(units: list[_Unit]) -> str:
    parts = []
    prev = None
    for u in units:
        if prev is not None:
            if u.line == prev.line:
                sep = "" if (u.cjk or prev.cjk) else " "
            elif prev.text.endswith("-") and not prev.cjk:
                parts[-1] = parts[-1][:-1]
                sep = ""
            else:
                sep = "" if (u.cjk or prev.cjk) else " "
            parts.append(sep)
        parts.append(u.text)
        prev = u
    return "".join(parts)


def units_rects(units: list[_Unit]) -> list[list[float]]:
    """一行合成一个框（PDF 坐标）。"""
    out: list[list[float]] = []
    last_line = None
    for u in units:
        if out and u.line == last_line:
            r = out[-1]
            r[0], r[1], r[2], r[3] = min(r[0], u.x0), min(r[1], u.y0), max(r[2], u.x1), max(r[3], u.y1)
        else:
            out.append([u.x0, u.y0, u.x1, u.y1])
        last_line = u.line
    return out


class _Canvas(QWidget):
    def __init__(self, view: "PdfView"):
        super().__init__(view.stage)
        self.v = view
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        self.v._paint(p, self.width(), self.height())

    def mousePressEvent(self, e) -> None:
        self.v._press(e)

    def mouseMoveEvent(self, e) -> None:
        self.v._move(e)

    def mouseReleaseEvent(self, e) -> None:
        self.v._release(e)

    def mouseDoubleClickEvent(self, e) -> None:
        self.v._double(e)

    def wheelEvent(self, e) -> None:
        self.v._wheel(e)

    def resizeEvent(self, e) -> None:
        self.v.popover.hide()
        self.v._relayout()


class PdfView(BookView):
    BG_KEY = "pdfdesk"
    DIM_KEY = "pdfdesk"

    def __init__(self, parent=None):
        super().__init__(parent)
        prefs = load_prefs()
        self._fit = prefs.get("pdf_fit", "width") if prefs.get("pdf_fit") in ("width", "page") else "width"
        self._paper = prefs.get("pdf_paper", "warm") if prefs.get("pdf_paper") in ("warm", "invert", "orig") \
            else "warm"
        self._spread = bool(prefs.get("pdf_spread", False))   # 要不要双页
        self._two = False                                     # 现在是不是真的两页并排
        self._renderer: Renderer | None = None
        self._sizes: list[tuple[float, float]] = []
        self._tops: list[float] = []
        self._ws: list[float] = []
        self._hs: list[float] = []
        self._xs: list[float] = []
        self._total_h = 0.0
        self._y = 0.0
        self._cache: OrderedDict = OrderedDict()
        self._cache_bytes = 0
        self._latest: dict[int, tuple] = {}     # 页 → 最近一张图的缓存键（换大小时先拿它顶着）
        self._units: OrderedDict[int, list[_Unit]] = OrderedDict()
        self._sel: tuple[int, int, int] | None = None     # (页, 起点字, 当前字)
        self._press_at: QPointF | None = None
        self._pop_target: tuple | None = None
        self._src: tuple[int, list] | None = None
        self._asked: tuple[int, list] | None = None
        self._errors: dict[int, str] = {}
        self._stats = {"paint_ms": [], "renders": 0}

        self.canvas = _Canvas(self)
        self.canvas.lower()
        self.sbar = QScrollBar(Qt.Vertical, self.stage)
        self.sbar.setFocusPolicy(Qt.NoFocus)
        self.sbar.valueChanged.connect(self._sbar_moved)
        self._sbar_guard = False
        self.stage.resized.connect(self._fit_canvas)
        self._ocr_timer = QTimer(self)
        self._ocr_timer.setInterval(3000)
        self._ocr_timer.timeout.connect(self._ocr_tick)
        self.bar.aa.setToolTip("大小、单页 / 双页、纸色")
        self.bar.aa.set_icon("down")                       # 「适合宽度 ⌄」：箭头在字后面
        self.bar.aa.setLayoutDirection(Qt.RightToLeft)

    # ── 接口 ──────────────────────────────────────────────
    def open(self, book: Book, loc: Loc | None, highlights: list[dict]) -> None:
        if self._renderer is not None:
            self._renderer.close()
        self.book = book
        self._hls = [dict(h) for h in highlights]
        self._cache.clear()
        self._cache_bytes = 0
        self._latest.clear()
        self._units.clear()
        self._sel = self._src = self._asked = None
        self._barrier = None
        self._last_loc = None
        self.pill.hide()
        self.hint.hide()
        self.popover.hide()
        doc = book.doc
        sizes = []
        for i in range(book.pages):
            try:
                r = doc.page_cropbox(i)
                w, h = abs(r.width), abs(r.height)
            except Exception:  # noqa: BLE001
                w, h = 0, 0
            if w < 1 or h < 1:
                r = doc[i].rect
                w, h = max(1.0, r.width), max(1.0, r.height)
            sizes.append((w, h))
        # 转过 90° 的页（少见）：按转过后的样子排
        for i in range(min(book.pages, 400)):
            try:
                rot = doc.xref_get_key(doc.page_xref(i), "Rotate")
            except Exception:  # noqa: BLE001
                rot = ("null", "")
            if rot[0] == "int" and int(rot[1]) % 180:
                sizes[i] = (sizes[i][1], sizes[i][0])
        self._sizes = sizes
        r = self._renderer = Renderer(str(book.path))
        r.ready.connect(self._got_image)
        r.failed.connect(self._render_failed)
        self.destroyed.connect(lambda *_: r.close())     # 书页没了，出图的子进程也收掉
        self._fit_canvas()
        self._relayout(keep=False)
        self.goto(loc or Loc(0))
        self._update_note()
        self._ocr_timer.start() if getattr(book, "text_source", "text") != "text" else self._ocr_timer.stop()
        self._emit_location()

    def goto(self, loc: Loc) -> None:
        if not self._ready():
            return
        p = self._row(max(0, min(loc.doc, len(self._tops) - 1)))[0]
        self._scroll_to(self._tops[p] - PAD_Y, user=False)

    def current_loc(self) -> Loc:
        if not self._ready():
            return Loc(0)
        return Loc(self._page_at(self._y + self._view_h() / 2))

    def page_label(self, loc: Loc) -> str:
        return f"第 {loc.doc + 1} 页"

    def mark_source(self, loc: Loc, quote: str) -> None:
        if not self._ready():
            return
        self.popover.hide()
        before_y = self._y
        before_label = self.page_label(self.current_loc())
        p = max(0, min((loc.doc if loc is not None else self.current_loc().doc), self.book.pages - 1))
        hit = self._find_quote(p, quote)
        self._src = hit
        if hit:
            q, rects = hit
            top = self._tops[q] + rects[0][1] * self._scale(q)
            self._scroll_to(top - self._view_h() / 3, user=False)
        else:
            self.goto(Loc(p))
        target = hit[0] if hit else p
        here = self.page_label(Loc(target))
        self._return_to = before_y
        self._src_label = here
        self.pill.show_jump(here, before_label if before_label != here else None)
        self.canvas.update()

    def _clear_source_mark(self) -> None:
        self._src = None

    def _go_back(self) -> None:
        y = self._return_to
        self.pill.hide()
        self._return_to = None
        self._act()
        if y is not None:
            self._scroll_to(y, user=False)

    def page_forward(self) -> None:
        self._scroll_by(self._view_h() - 60)

    def page_back(self) -> None:
        self.hint.hide()
        self._scroll_by(-(self._view_h() - 60))

    def next_page(self) -> None:
        """底栏的 ›：下一页的顶上。"""
        p = self._row(self._page_at(self._y + PAD_Y + 1))[-1]    # 双页：跳过同一排的右页
        if p + 1 < len(self._tops):
            self._scroll_by(self._tops[p + 1] - PAD_Y - self._y)

    def prev_page(self) -> None:
        self.hint.hide()
        p = self._page_at(self._y + PAD_Y + 1)
        cur_top = self._tops[p] - PAD_Y
        target = self._tops[p - 1] - PAD_Y if (self._y - cur_top) < 4 and p > 0 else cur_top
        self._scroll_by(target - self._y)

    def _user_turn(self, d: int) -> None:
        self._act()
        self.next_page() if d > 0 else self.prev_page()

    def _scroll_lines(self, n: int) -> None:
        self._scroll_by(n * 60)

    def set_fit(self, fit: str) -> None:
        if fit == self._fit or fit not in ("width", "page"):
            return
        self._fit = fit
        save_prefs(pdf_fit=fit)
        self._relayout()
        self._update_bar()

    def set_spread(self, on: bool) -> None:
        """左右两页并排（窗口不够宽就先单页）。"""
        on = bool(on)
        if on == self._spread:
            return
        self._spread = on
        save_prefs(pdf_spread=on)
        self._sel = None
        self.popover.hide()
        self._relayout()
        self._update_bar()

    def spread(self) -> bool:
        return self._spread

    def two_pages(self) -> bool:
        """现在是不是真的两页并排。"""
        return self._two

    def set_paper(self, paper: str) -> None:
        if paper == self._paper or paper not in ("warm", "invert", "orig"):
            return
        self._paper = paper
        save_prefs(pdf_paper=paper)
        self.canvas.update()
        self._request()

    def fit(self) -> str:
        return self._fit

    def paper(self) -> str:
        return self._paper

    # ── 排版 ──────────────────────────────────────────────
    def _ready(self) -> bool:
        return self.book is not None and bool(self._tops)

    def _dpr(self) -> float:
        return self.canvas.devicePixelRatioF() or 1.0

    def _view_h(self) -> float:
        return float(max(100, self.canvas.height()))

    def _scale(self, i: int) -> float:
        return self._ws[i] / self._sizes[i][0]

    def _fit_canvas(self) -> None:
        r = self.stage.rect()
        self.canvas.setGeometry(r)
        self.sbar.setGeometry(r.width() - 12, 2, 10, r.height() - 4)
        self.sbar.raise_()
        self.pill.raise_()
        self.hint.raise_()
        self.popover.raise_()
        self.dim.raise_()

    def _relayout(self, keep: bool = True) -> None:
        if self.book is None or not self._sizes:
            return
        anchor = None
        if keep and self._tops:
            p = self._page_at(self._y + PAD_Y)
            anchor = (p, (self._y - self._tops[p]) / max(1.0, self._hs[p]))
        W = max(200, self.canvas.width() - 12)
        H = self._view_h()
        avail = max(160.0, W - 2 * PAD_X)
        dpr = self._dpr()
        self._two = two = self._spread and (avail - SPREAD_GAP) / 2 >= SPREAD_MIN_W
        col = (avail - SPREAD_GAP) / 2 if two else avail     # 一页最多多宽
        tops, ws, hs, xs = [], [], [], []
        y = PAD_Y
        n = len(self._sizes)
        i = 0
        while i < n:
            row = [i, i + 1] if two and i + 1 < n else [i]   # 双页：一排两页，顶端对齐
            top = round(y * dpr) / dpr
            row_h = 0.0
            for k, j in enumerate(row):
                pw, ph = self._sizes[j]
                s = col / pw if self._fit == "width" else min(col / pw, (H - 2 * PAD_Y) / ph)
                w = round(pw * s * dpr) / dpr
                h = round(ph * s * dpr) / dpr
                if not two:
                    x = (W - w) / 2
                elif k == 0:
                    x = W / 2 - SPREAD_GAP / 2 - w          # 左页贴着中缝
                else:
                    x = W / 2 + SPREAD_GAP / 2
                tops.append(top)
                ws.append(w)
                hs.append(h)
                xs.append(round(x * dpr) / dpr)
                row_h = max(row_h, h)
            y += row_h + GAP
            i += len(row)
        self._tops, self._ws, self._hs, self._xs = tops, ws, hs, xs
        self._total_h = y - GAP + PAD_Y
        if anchor:
            p, frac = anchor
            self._y = self._tops[p] + frac * self._hs[p]
        self._scroll_to(self._y, user=False)

    def _page_at(self, y: float) -> int:
        """y 所在那一排的第一页（双页时是左页）。"""
        i = max(0, min(len(self._tops) - 1, bisect_right(self._tops, y) - 1))
        while i > 0 and self._tops[i - 1] == self._tops[i]:
            i -= 1
        return i

    def _row(self, i: int) -> list[int]:
        """跟第 i 页在同一排的页（单页时就是它自己）。"""
        if not self._two or not self._tops:
            return [i]
        return [j for j in (i - 1, i, i + 1) if 0 <= j < len(self._tops) and self._tops[j] == self._tops[i]]

    def _shown(self, i: int) -> bool:
        """这一页画不画：挡板模式下过了挡板的不画（双页时挡板那一排的右页可能露在外面）。"""
        if not self.BLOCK:
            return True
        bl = self._barrier_loc()
        return bl is None or i < bl.doc

    def _max_y(self) -> float:
        return max(0.0, self._total_h - self._view_h())

    def _barrier_y(self) -> float | None:
        bl = self._barrier_loc()
        if bl is None:
            return None
        last = max(0, min(bl.doc - 1, len(self._tops) - 1))
        # 最多滚到这一页（这一排）的底边再露一点页缝，下一页（下一排）一点都看不到
        bottom = max(self._tops[j] + self._hs[j] for j in self._row(last))
        return max(0.0, bottom + GAP - 2 - self._view_h())

    def _toc_allowed(self, loc: Loc) -> bool:
        bl = self._barrier_loc()
        return not self.BLOCK or bl is None or loc.doc < bl.doc

    # ── 滚动 ──────────────────────────────────────────────
    def _scroll_by(self, dy: float) -> None:
        self.popover.hide()
        self._scroll_to(self._y + dy, user=True)

    def _scroll_to(self, y: float, user: bool = True) -> None:
        y = max(0.0, min(y, self._max_y()))
        if user and y > self._y:
            by = self._barrier_y()
            if by is not None and y > by and self._blocked():
                y = max(self._y, by)
        elif user and self.hint.isVisible() and y < self._y:
            self.hint.hide()
        changed = abs(y - self._y) > 0.01
        self._y = y
        self._sel = None if changed else self._sel
        self._sbar_guard = True
        self.sbar.setRange(0, int(self._max_y()))
        self.sbar.setPageStep(int(self._view_h()))
        self.sbar.setSingleStep(60)
        self.sbar.setValue(int(round(y)))
        self._sbar_guard = False
        self.canvas.update()
        self._request()
        self._update_bar()
        self._emit_location()

    def _sbar_moved(self, v: int) -> None:
        if self._sbar_guard:
            return
        self._act()
        self._continuous_input(self._scroll_to, float(v), True)

    def _wheel(self, e) -> None:
        self._act()
        pd = e.pixelDelta().y()
        dy = -pd if pd else -e.angleDelta().y() / 120 * 100
        self._continuous_input(self._scroll_by, dy)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Home:
            self._act()
            self._scroll_to(0, user=False)
        elif e.key() == Qt.Key_End:
            self._act()
            self._scroll_to(self._max_y(), user=True)
        else:
            super().keyPressEvent(e)

    # ── 出图、缓存 ─────────────────────────────────────────
    def _colors(self):
        """当前纸色 → 给出图用的颜色参数。"""
        def rgb(key):
            c = theme.color(key)
            return (c.red() << 16) | (c.green() << 8) | c.blue()
        if theme.is_light():
            return ("tint", rgb("pdfink"), rgb("pdfpaper"))
        if self._paper == "invert":
            return ("invert", rgb("paper"), rgb("ink"))
        if self._paper == "orig":
            return None
        return ("tint", rgb("pdfink"), rgb("pdfpaper"))

    def _placeholder(self):
        if not theme.is_light() and self._paper == "invert":
            return theme.color("paper")
        return theme.color("pdfpaper")

    def _visible(self) -> list[int]:
        if not self._tops:
            return []
        y0, y1 = self._y, self._y + self._view_h()
        i = self._page_at(y0)
        out = []
        while i < len(self._tops) and self._tops[i] < y1:
            if self._tops[i] + self._hs[i] > y0 and self._shown(i):
                out.append(i)
            i += 1
        return out

    def _request(self) -> None:
        if self._renderer is None or not self._tops:
            return
        vis = self._visible()
        if not vis:
            return
        mid = self._y + self._view_h() / 2
        order = sorted(vis, key=lambda i: abs(self._tops[i] + self._hs[i] / 2 - mid))
        extra = [vis[-1] + 1, vis[-1] + 2, vis[0] - 1]
        order += [i for i in extra if 0 <= i < len(self._tops) and i not in order]
        colors = self._colors()
        dpr = self._dpr()
        jobs = []
        for i in order:
            px = round(self._ws[i] * dpr)
            if (i, px, colors) not in self._cache and i not in self._errors:
                jobs.append((i, px, colors))
        self._renderer.want(jobs)

    def _got_image(self, page: int, px_w: int, colors, img: QImage) -> None:
        key = (page, px_w, colors)
        if key in self._cache:
            return
        self._stats["renders"] += 1
        self._cache[key] = img
        self._cache_bytes += img.sizeInBytes()
        self._latest[page] = key
        vis = set(self._visible())
        while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
            k = next(iter(self._cache))
            if k[0] in vis and k == (k[0], round(self._ws[k[0]] * self._dpr()), self._colors()):
                self._cache.move_to_end(k)
                if all(x[0] in vis for x in self._cache):
                    break
                continue
            old = self._cache.pop(k)
            self._cache_bytes -= old.sizeInBytes()
            if self._latest.get(k[0]) == k:
                self._latest.pop(k[0], None)
        if page in vis:
            self.canvas.update()

    def _render_failed(self, page: int, why: str) -> None:
        self._errors[page] = why
        self.canvas.update()

    def pending(self) -> int:
        """看得见的页里还有几页没出图（测试、测速用）。"""
        colors, dpr = self._colors(), self._dpr()
        return sum(1 for i in self._visible() if (i, round(self._ws[i] * dpr), colors) not in self._cache
                   and i not in self._errors)

    # ── 画 ────────────────────────────────────────────────
    def _paint(self, p: QPainter, w: int, h: int) -> None:
        t0 = time.perf_counter()
        p.fillRect(0, 0, w, h, theme.color("pdfdesk"))
        if not self._ready():
            return
        dpr = self._dpr()
        colors = self._colors()
        shadow = theme.color("shadow")
        for i in self._visible():
            r = QRectF(self._xs[i], self._tops[i] - self._y, self._ws[i], self._hs[i])
            r.moveTop(round(r.top() * dpr) / dpr)
            # 纸的阴影（界面稿 0 2px 12px）：几圈越来越淡的边
            for k, a in ((6, 0.10), (4, 0.14), (2, 0.20)):
                c = QColor(shadow)
                c.setAlphaF(shadow.alphaF() * a)
                p.fillRect(r.adjusted(-k, -k + 2, k, k + 2), c)
            key = (i, round(self._ws[i] * dpr), colors)
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
                img.setDevicePixelRatio(dpr)
                p.drawImage(r.topLeft(), img)
            else:
                old = self._latest.get(i)
                oimg = self._cache.get(old) if old else None
                if oimg is not None and old[2] == colors:
                    p.save()
                    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                    p.drawImage(r, oimg, QRectF(0, 0, oimg.width(), oimg.height()))
                    p.restore()
                else:
                    p.fillRect(r, self._placeholder())
                    if i in self._errors:
                        p.setPen(theme.color("pdfink2"))
                        p.setFont(theme.font(13))
                        p.drawText(r, Qt.AlignCenter, f"第 {i + 1} 页出图失败了")
            self._paint_marks(p, i, r)
        self._stats["paint_ms"].append(1000 * (time.perf_counter() - t0))
        if len(self._stats["paint_ms"]) > 2000:
            del self._stats["paint_ms"][:1000]

    def _to_screen(self, i: int, rect) -> QRectF:
        s = self._scale(i)
        return QRectF(self._xs[i] + rect[0] * s, self._tops[i] - self._y + rect[1] * s,
                      (rect[2] - rect[0]) * s, (rect[3] - rect[1]) * s)

    def _paint_marks(self, p: QPainter, i: int, _page_rect: QRectF) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        hl = theme.color("pdfhl")
        for h in self._hls:
            if h.get("page") == i:
                for rc in h.get("rects", []):
                    p.setBrush(hl)
                    p.drawRoundedRect(self._to_screen(i, rc).adjusted(-1, -1, 1, 1), 2, 2)
        if self._src and self._src[0] == i:
            src, acc = theme.color("src"), theme.color("acc")
            for rc in self._src[1]:
                r = self._to_screen(i, rc).adjusted(-1, -1, 1, 1)
                p.setBrush(src)
                p.drawRoundedRect(r, 2, 2)
                p.fillRect(QRectF(r.left(), r.bottom() - 2, r.width(), 2), acc)
        if self._sel and self._sel[0] == i:
            p.setBrush(theme.color("sel"))
            for rc in units_rects(self._sel_units()):
                p.drawRoundedRect(self._to_screen(i, rc).adjusted(-1, -1, 1, 1), 2, 2)
        if self._asked and self._asked[0] == i:
            pen = QPen(theme.color("acc"), 1.5)
            pen.setDashPattern([1.0, 2.0])
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            for rc in self._asked[1]:
                r = self._to_screen(i, rc)
                p.drawLine(QPointF(r.left(), r.bottom() + 3), QPointF(r.right(), r.bottom() + 3))
        p.restore()

    # ── 选字 ──────────────────────────────────────────────
    def _page_units(self, i: int) -> list[_Unit]:
        got = self._units.get(i)
        if got is not None:
            self._units.move_to_end(i)
            return got
        try:
            words = self.book.page_words(i) if hasattr(self.book, "page_words") else \
                [(w[0], w[1], w[2], w[3], w[4], w[5] * 1000 + w[6]) for w in self.book.doc[i].get_text("words")]
        except Exception:  # noqa: BLE001
            words = []
        units = page_units(words)
        if units:   # 没有字的页不记：扫描版过一会儿可能就识别出来了
            self._units[i] = units
            while len(self._units) > 40:
                self._units.popitem(last=False)
        return units

    def _pdf_point(self, pt: QPointF) -> tuple[int, float, float]:
        i = self._page_at(self._y + pt.y())
        row = [j for j in self._row(i) if self._shown(j)] or [i]
        if len(row) == 2 and pt.x() >= (self._xs[row[0]] + self._ws[row[0]] + self._xs[row[1]]) / 2:
            i = row[1]                 # 点在右页
        s = self._scale(i)
        return i, (pt.x() - self._xs[i]) / s, (self._y + pt.y() - self._tops[i]) / s

    @staticmethod
    def _unit_at(units: list[_Unit], x: float, y: float, strict: bool) -> int | None:
        best, bd = None, 1e18
        for k, u in enumerate(units):
            if u.x0 - 1 <= x <= u.x1 + 1 and u.y0 - 1 <= y <= u.y1 + 1:
                return k
            if strict:
                continue
            dy = 0 if u.y0 <= y <= u.y1 else min(abs(y - u.y0), abs(y - u.y1))
            dx = 0 if u.x0 <= x <= u.x1 else min(abs(x - u.x0), abs(x - u.x1))
            dist = dy * 4 + dx     # 先按行、再按左右
            if dist < bd:
                best, bd = k, dist
        return best

    def _sel_units(self) -> list[_Unit]:
        if not self._sel:
            return []
        i, a, b = self._sel
        units = self._page_units(i)
        a, b = min(a, b), max(a, b)
        return units[a:b + 1]

    def _press(self, e) -> None:
        self._act()
        self.setFocus(Qt.MouseFocusReason)
        self.popover.hide()
        self._pop_target = None
        if e.button() != Qt.LeftButton or not self._ready():
            return
        self._press_at = e.position()
        i, x, y = self._pdf_point(e.position())
        units = self._page_units(i)
        k = self._unit_at(units, x, y, strict=True) if units else None
        self._sel = (i, k, k) if k is not None else None
        self._sel_page = i
        self.canvas.update()

    def _move(self, e) -> None:
        if not (e.buttons() & Qt.LeftButton) or self._press_at is None:
            return
        if (e.position() - self._press_at).manhattanLength() < 4:
            return
        page = getattr(self, "_sel_page", None)
        if page is None:
            return
        _, x, y = self._pdf_point(e.position())
        # 拖出这一页也按这一页算（不跨页选）
        s = self._scale(page)
        y = (self._y + e.position().y() - self._tops[page]) / s
        x = (e.position().x() - self._xs[page]) / s
        units = self._page_units(page)
        if not units:
            return
        k = self._unit_at(units, x, y, strict=False)
        if self._sel is None:
            k0 = self._unit_at(units, x, y, strict=True)
            if k0 is None:
                return
            self._sel = (page, k0, k0)
        self._sel = (page, self._sel[1], k)
        self.canvas.update()

    def _release(self, e) -> None:
        if e.button() != Qt.LeftButton:
            return
        moved = self._press_at is not None and (e.position() - self._press_at).manhattanLength() >= 4
        if self._sel and moved:
            self._finish_selection()
            return
        self._sel = None
        # 单击：点在高亮上 → 「问 AI · 取消高亮」
        if self._ready():
            i, x, y = self._pdf_point(e.position())
            for h in self._hls:
                if h.get("page") == i and any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in h.get("rects", [])):
                    self._show_pop(i, h["rects"], ("hl", h))
                    return
        self.canvas.update()

    def _double(self, e) -> None:
        """双击：中文选一句，英文选一个词。"""
        self._act()
        if not self._ready():
            return
        i, x, y = self._pdf_point(e.position())
        units = self._page_units(i)
        k = self._unit_at(units, x, y, strict=True) if units else None
        if k is None:
            return
        if units[k].cjk:
            ends = "。！？!?；;"
            a = k
            while a > 0 and units[a - 1].text[-1:] not in ends:
                a -= 1
            b = k
            while b < len(units) - 1 and units[b].text[-1:] not in ends:
                b += 1
        else:
            a = b = k
        self._sel = (i, a, b)
        self._finish_selection()

    def _finish_selection(self) -> None:
        units = self._sel_units()
        if not units:
            self._sel = None
            self.canvas.update()
            return
        self._act()
        self._show_pop(self._sel[0], units_rects(units), ("sel",))

    def _show_pop(self, i: int, rects: list, target: tuple) -> None:
        if not rects:
            return
        scr = [self._to_screen(i, r) for r in rects]
        self._pop_target = (i, rects) + target
        self.popover.show_for(scr[0], scr[-1], existing=target[0] == "hl")
        self.canvas.update()

    def _pop_ask(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if not t:
            return
        i, rects = t[0], t[1]
        text = units_text(self._sel_units()) if t[2] == "sel" else t[3].get("text", "")
        self._asked = (i, [list(r) for r in rects])
        self._sel = None
        self.canvas.update()
        self._act()
        self.ask_ai.emit(text, Loc(i))

    def _pop_highlight(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if not t or t[2] != "sel":
            return
        units = self._sel_units()
        self.add_highlight(t[0], units_rects(units), units_text(units))
        self._sel = None
        self.canvas.update()

    def _pop_unhighlight(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if t and t[2] == "hl":
            self.remove_highlight(t[3])

    def add_highlight(self, page: int, rects: list, text: str) -> dict:
        h = {"page": page, "rects": [[round(v, 2) for v in r] for r in rects], "text": text}
        self._hls.append(h)
        self._update_bar()
        self.canvas.update()
        self.highlights_changed.emit()
        return h

    def remove_highlight(self, h: dict) -> None:
        n = len(self._hls)
        self._hls = [x for x in self._hls if not (x is h or x == h)]
        self._update_bar()
        self.canvas.update()
        if len(self._hls) != n:
            self.highlights_changed.emit()

    def _hl_items(self):
        items = []
        for h in sorted(self._hls, key=lambda x: (x.get("page", 0), (x.get("rects") or [[0, 0]])[0][1])):
            text = h.get("text", "").replace("\n", " ")
            text = text[:44] + "…" if len(text) > 45 else text
            items.append((f"{self.page_label(Loc(h['page']))}　{text}", 1, h, True, False, ""))
        return items

    def _goto_highlight(self, h: dict) -> None:
        i = h.get("page", 0)
        rects = h.get("rects") or [[0, 0, 0, 0]]
        self._scroll_to(self._tops[i] + rects[0][1] * self._scale(i) - self._view_h() / 3, user=False)

    # ── 出处 ──────────────────────────────────────────────
    def _find_quote(self, p: int, quote: str) -> tuple[int, list] | None:
        quote = (quote or "").strip()
        if not quote:
            return None
        pages = [q for q in (p, p + 1, p - 1) if 0 <= q < self.book.pages]
        if getattr(self.book, "has_text", True):
            for q in pages:
                try:
                    hits = self.book.doc[q].search_for(quote)
                except Exception:  # noqa: BLE001
                    hits = []
                if hits:
                    return q, [[r.x0, r.y0, r.x1, r.y1] for r in hits]
        qn, _ = norm_chars(quote)
        if len(qn) < 2:
            return None
        for fuzzy in (False, True):
            for q in pages:
                units = self._page_units(q)
                if not units:
                    continue
                chars, idx = [], []
                for k, u in enumerate(units):
                    s, _ = norm_chars(u.text)
                    chars.append(s)
                    idx.extend([k] * len(s))
                got = find_in(qn, "".join(chars), idx, fuzzy)
                if got:
                    a, b = got[0], got[1] - 1
                    return q, units_rects(units[a:b + 1])
        return None

    # ── 底栏 ──────────────────────────────────────────────
    def update_view(self) -> None:
        self.canvas.update()
        self._update_bar()

    def _update_bar(self) -> None:
        if not self._ready():
            self.bar.set_page(None, None)
            return
        cur = self.current_loc().doc
        n = self.book.pages
        last = max(j for j in self._row(cur) if self._shown(j) or j == cur)   # 双页：右页的页码
        self.bar.set_page(cur + 1, n, last=last + 1)
        title = self._chapter_title(Loc(cur))
        pct = round(100 * (last + 1) / n)
        self.bar.set_info(f"{title} · {pct}%" if title else f"{pct}%")
        self.bar.set_hl_count(len(self._hls))
        self.bar.aa.setText(("适合宽度" if self._fit == "width" else "整页") + (" · 双页" if self._two else ""))
        self.bar.prev.setEnabled(self._y > 0.5)
        self.bar.next.setEnabled(cur < n - 1 or self._y < self._max_y() - 0.5)

    def _update_note(self) -> None:
        """扫描版：底栏写明能不能选字。"""
        src = getattr(self.book, "text_source", "text")
        if src == "text":
            self.bar.set_note(None)
            return
        done, total = self.book.ocr_progress() if hasattr(self.book, "ocr_progress") else (0, self.book.pages)
        can_ocr = getattr(self.book, "ocr", None) is not None
        # 「还在识别」只在后台真的在识别这本书时说（书架的识别可能在做别的书，或者退出时停了）
        running = can_ocr and done < total and bool(getattr(self.book, "ocr_running", lambda: False)())
        if src == "none":
            if running:
                self.bar.set_note("扫描版，还在识别文字…",
                                  "识别好的页就能选字、划线了；现在只能看", hide_hl=True)
            elif can_ocr:
                self.bar.set_note(f"扫描版 · 已识别 0 / {total} 页",
                                  "文字还没识别，现在只能看；书架上会在后台接着识别", hide_hl=True)
            else:
                self.bar.set_note("扫描版", "这本 PDF 没有文字层，这台电脑也识别不了：只能看，不能选字和划线",
                                  hide_hl=True)
            return
        if done < total:
            if running:
                self.bar.set_note(f"扫描版 · 还在识别文字… {done} / {total} 页", "识别好的页能选字、划线")
            else:
                self.bar.set_note(f"扫描版 · 已识别 {done} / {total} 页",
                                  "识别好的页能选字、划线；没识别的页只能看")
        else:
            self.bar.set_note(None)

    def _ocr_tick(self) -> None:
        if self.book is None:
            self._ocr_timer.stop()
            return
        self._update_note()
        done, total = self.book.ocr_progress() if hasattr(self.book, "ocr_progress") else (0, 0)
        if getattr(self.book, "text_source", "text") == "text" or done >= total:
            self._ocr_timer.stop()

    def _build_aa(self, panel: AaPanel) -> None:
        seg = Seg([("width", "适合宽度"), ("page", "整页")], self._fit)
        seg.changed.connect(lambda f: (self._act(), self.set_fit(f)))
        panel.add_row("大小", seg)
        two = Seg([("one", "单页"), ("two", "双页")], "two" if self._spread else "one")
        two.buttons["two"].setToolTip("左右两页并排，像摊开的书（配「整页」最像）")
        note = QLabel("窗口不够宽，先按单页显示")
        note.setFont(theme.font(12))
        note.setStyleSheet(theme.qss("color: {t4};"))

        def _pick(key: str) -> None:
            self._act()
            self.set_spread(key == "two")
            note.setVisible(self._spread and not self._two)
            panel.open_at(self.bar.aa, "right")     # 多了 / 少了一行，重新摆一下
        two.changed.connect(_pick)
        panel.add_row("显示", two)
        panel.grid.addWidget(note, panel.grid.rowCount(), 1)
        note.setVisible(self._spread and not self._two)
        if not theme.is_light():
            paper = Seg([("warm", "暖纸"), ("invert", "反色"), ("orig", "原样")], self._paper)
            paper.changed.connect(lambda m: (self._act(), self.set_paper(m)))
            panel.add_row("纸色", paper)

    def _theme_changed(self) -> None:
        # 颜色换了：旧图作废
        self._cache.clear()
        self._cache_bytes = 0
        self._latest.clear()
        self.canvas.update()
        self._request()
        self.update_view()

    def closeEvent(self, e) -> None:
        if self._renderer is not None:
            self._renderer.close()
        super().closeEvent(e)

    def __del__(self):
        try:
            if self._renderer is not None:
                self._renderer.close()
        except Exception:  # noqa: BLE001
            pass

    # ── 演示用 ─────────────────────────────────────────────
    def demo_select(self) -> None:
        """在看得见的页中间选一句，弹出小条。"""
        if not self._ready():
            return
        i = self._page_at(self._y + self._view_h() * 0.45)
        units = self._page_units(i)
        if not units:
            return
        _, x, y = self._pdf_point(QPointF(self.canvas.width() / 2, self._view_h() * 0.45))
        k = self._unit_at(units, x, y, strict=False)
        a = k
        while a > 0 and units[a - 1].line == units[k].line:
            a -= 1
        b = k
        while b < len(units) - 1 and units[b + 1].line in (units[k].line, units[k].line + 1):
            b += 1
        self._sel = (i, a, b)
        self._finish_selection()


if __name__ == "__main__":
    import sys

    from .base import demo_main
    sys.exit(demo_main("pdf", "~/Downloads/*.pdf", sys.argv[1:]))
