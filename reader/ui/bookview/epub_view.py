"""EPUB 书页：Qt 自己排版（QTextDocument + FreeType + MiSans），不用 QWebEngine（ClearType 彩边，见 SPEC）。

## 排版（界面稿板 3）
- 默认 MiSans 18px、行高 1.85、每行约 34 字（栏宽 34em，居中）、段首空两格、两端对齐、段后 0.85em。
  标题按级别放大、加粗、不缩进。Aa 里能改（都记住）：字体（黑体 / 宋体 / 楷体，电脑上有才给选）、
  字号 14–72px（Ctrl + 滚轮、Ctrl + = / −）、粗细三档、行距（紧 1.6 / 适中 1.85 / 松 2.1）、
  每行字数（28 / 34 / 42）、恢复默认。正文行距按像素定死（换字体、回退字体不会把个别行撑高）；
  栏宽取字号的整数倍（clreq：整行汉字两端对齐时字距不被拉开）。
- 中文排版的小修（字数都不变，位置不挪）：挨着汉字的弯引号补足到一个字宽（MiSans 的引号是西文窄的）；
  中文标点前、注释编号前转制留下的空格粘住（不再把「，」挤到行首）；「[1]」这种注释编号排成上标。
- 图片块从 `EpubBook.image_bytes` 取：排版时只读图片尺寸，画到那一页才解码（按设备像素缩好）。

## 一章一个文档
每个 spine 文件（通常是一章）排成一个 QTextDocument：书的块和文档的段一一对应（第 b 块 = 第 b 段），
所以 `Loc(doc, block)` ↔ 文档里的字符位置可以直接互换；块内偏移就是字符偏移（换行 `\\n` 换成同样一个字的
行分隔符）。一章从新的一页开始，跟纸书一样。

- 分页：`setPageSize(栏宽, 页高)`，Qt 自己把跨页的行推到下一页；排完记下每页第一个字的位置（页表）。
  当前这章马上排，其余的章在空闲时一章一章排（每次几十毫秒，不卡界面），排完页码就是准的
  （没排完时底栏写「约」）。页码 = 前面各章页数之和 + 章内页号 —— 按当前字号和窗口大小算。
- 滚动：同样的内容另排一份不分页的，一章接一章往下画。页码仍按分页的页表算，两种方式页码一致。
- 双页（Aa 里选）：分页的一种，左右两栏各是一页，像摊开的书；每章从左页开始（章末单出来的一页右边空着）。
  窗口不够宽（一栏排不下 SPREAD_MIN_EM 个字）时照单页排，窗口拉宽了自己变回双页。页码还是一栏算一页。

## 换字号、换窗口大小不挪位置
记着一个「读到这儿」的字（`_anchor`）：翻页时是这一页（双页时是这两页）上第一个开头的段，跳转时是跳到的那个字。
重排时回到这个字所在的页，而且不因为重排改写它 —— 不然每换一次字号就往前漂一两段，漂过节首就连顶条的节名也错了。
重排后当前这章马上排好，它前面的章先排（排完页码就准了），没排的章按「重排前的页数 × 这次变了多少」估。

## 标记（都画在字的下面，颜色从 theme 取）
选区 `sel` 蓝灰、高亮 `hl` 黄、出处 `src` 陶土色底 + 2px `acc` 下划线、问过 AI 的那段 `acc` 细虚线。
双击选一句，三击选一段。
"""

from __future__ import annotations

import re
import time
import unicodedata
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSizeF, Qt, QTimer, QUrl
from PySide6.QtGui import (QAbstractTextDocumentLayout, QFont, QFontMetricsF, QImage, QImageReader, QPainter, QPalette, QPen,
                           QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat)
from PySide6.QtWidgets import QLabel, QPushButton, QScrollBar, QWidget

from reader.books import Book, Loc
from reader.ui import theme

from .base import AaPanel, BookView, Seg, Stepper, find_in, load_prefs, norm_chars, save_prefs

PAD_TOP = 34        # 界面稿 .page padding-top
PAD_BOTTOM = 22
PAD_SIDE = 40
# 字号不设小的上限（2026-09-25 起：「不要限制最大号的字体」）：大到一行只排得下十来个字也照排
FONT_SIZES = [14, 15, 16, 17, 18, 20, 22, 24, 26, 28, 30, 32, 36, 40, 44, 48, 56, 64, 72]
LINES = {"tight": 1.6, "normal": theme.BOOK_LINE, "loose": 2.1}          # 行距（CSS 的倍数）
WIDTHS = {"narrow": 28, "normal": theme.BOOK_COLUMN_EM, "wide": 42}      # 一行最多排多少个字
HEAD_EM = {"h1": 1.55, "h2": 1.33, "h3": 1.22, "h4": 1.11, "h5": 1.0, "h6": 1.0}
HEAD_LINE = 1.4     # 标题行高（CSS 值）
CH_GAP_EM = 4       # 滚动时两章之间空多少
GUTTER = 72         # 双页时两栏中间空多宽（中间画一条细线当书脊）
SPREAD_MIN_EM = 20  # 双页时一栏至少排得下这么多字，不够就先单页
RATIO_PRIOR = 30    # 重排后估页数：按版面算的比例当作先排好了这么多页（见 _estimates）
_LS = "\u2028"      # 行分隔符：跟 \n 一样占一个字，但不另起一段
_OBJ = "\ufffc"     # 图片在文档里占的那个字


_ZW = chr(0x200B)   # 零宽空格：只给 Qt 一个可以换行的地方，不占位置
_OPEN_Q = "“‘"
_CLOSE_Q = "”’"
_QUOTES = re.compile("[“‘”’]")


def break_points(text: str) -> list[int]:
    """中文里的弯引号旁边补上可以换行的地方（在 text[i] 前面插零宽空格的那些 i）。

    Qt 按 Unicode 老规则把 “” 当成两边都不能断的字符，于是「谴责”（Example，」这样一串整个挪到下一行，
    上一行两端对齐时每个字之间拉出很大的空（浏览器按新规则 LB19a 能断开）。这里只补两种：
    汉字（或中文标点）后面的开引号前面、闭引号后面紧跟汉字或开括号 —— 都不会让标点跑到行首。
    """
    out = []
    for m in _QUOTES.finditer(text):
        i = m.start()
        c = text[i]
        if c in _OPEN_Q and i > 0:
            a = text[i - 1]
            if unicodedata.east_asian_width(a) in "WF" and unicodedata.category(a) not in ("Ps", "Pi"):
                out.append(i)
        elif c in _CLOSE_Q and i + 1 < len(text):
            b = text[i + 1]
            cat = unicodedata.category(b)
            if unicodedata.east_asian_width(b) in "WF" and (cat[0] == "L" or cat in ("Ps", "Pi")):
                out.append(i + 1)
    return out


_GLUE = " "   # 粘住的空格（窄的不换行空格；排的时候再压成几乎没有宽度，复制出去还是空格）
_NOTE_REF = re.compile(r"\[\d{1,3}\]|［\d{1,3}］|〔\d{1,3}〕")


def glue_punct(text: str) -> str:
    """中文标点旁边、注释编号前面的空格换成粘住的空格（字数不变，位置不挪）。

    转制的书常在注释编号后面留个空格（「战争 [1] ，那么」），Qt 就在空格那儿换行，逗号跑到了行首。
    中文排版不让「，。、」这类标点出现在行首、开括号开引号出现在行尾（clreq 的行首行尾禁则）。"""
    if " " not in text:
        return text
    out = list(text)
    n = len(text)
    for m in _NOTE_REF.finditer(text):     # 「战争 [1]」：编号跟着前面的字走，不单独掉到下一行
        if m.start() and text[m.start() - 1] == " ":
            out[m.start() - 1] = _GLUE
    for i, c in enumerate(text):
        if c != " ":
            continue
        b = text[i + 1] if i + 1 < n else ""
        a = text[i - 1] if i else ""
        if (b and unicodedata.east_asian_width(b) in "WF" and unicodedata.category(b) in ("Pe", "Po", "Pf")) or                 (a and unicodedata.east_asian_width(a) in "WF" and unicodedata.category(a) in ("Ps", "Pi")):
            out[i] = _GLUE
    return "".join(out)


def cjk_quotes(text: str) -> list[int]:
    """挨着汉字的弯引号（开引号后面、闭引号前面是中文）：这些要用全角的字形。引英文的不动。"""
    def cjk(c: str) -> bool:
        return bool(c) and unicodedata.east_asian_width(c) in "WF"
    out = []
    for m in _QUOTES.finditer(text):
        i = m.start()
        c = text[i]
        if c in _OPEN_Q:
            j = i + 1
            while j < len(text) and text[j] == _ZW:
                j += 1
            if j < len(text) and cjk(text[j]):
                out.append(i)
        else:
            j = i - 1
            while j >= 0 and text[j] == _ZW:
                j -= 1
            if j >= 0 and cjk(text[j]):
                out.append(i)
    return out


def with_breaks(text: str, ins: list[int]) -> str:
    if not ins:
        return text
    parts, last = [], 0
    for i in ins:
        parts.append(text[last:i])
        parts.append(_ZW)
        last = i
    parts.append(text[last:])
    return "".join(parts)


class _ChapterDoc(QTextDocument):
    """一章的文档。图片画到才解码（loadResource），按设备像素缩好，边缘清楚。"""

    def __init__(self, book: Book, dpr: float):
        super().__init__()
        self.book = book
        self.dpr = dpr
        self.imgs: dict[str, tuple[str, float, float]] = {}
        self.ins: dict[int, list[int]] = {}     # 第 n 段在书的文字里哪些位置前面插了零宽空格

    def loadResource(self, typ, url: QUrl):
        meta = self.imgs.get(url.toString())
        if meta is not None:
            src, w, h = meta
            data = self.book.image_bytes(src)
            img = QImage.fromData(data) if data else QImage()
            if not img.isNull():
                tw, th = max(1, round(w * self.dpr)), max(1, round(h * self.dpr))
                if (img.width(), img.height()) != (tw, th):
                    img = img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                img.setDevicePixelRatio(self.dpr)
                return img
        return super().loadResource(typ, url)


@dataclass
class _Chapter:
    d: int
    chars: int
    doc: _ChapterDoc | None = None          # 分页排版
    pages: list[int] = field(default_factory=list)   # 每页第一个字的位置
    sdoc: _ChapterDoc | None = None         # 滚动排版
    sh: float = 0.0                         # 滚动排版的高度


@dataclass(frozen=True)
class BookStyle:
    """排版的样式（Aa 里选的，字号另外算）。face：sans 黑体 / serif 宋体 / kai 楷体；weight：0 细 1 标准 2 粗。"""
    face: str = "sans"
    weight: int = 1
    line: float = theme.BOOK_LINE


def _image_size(data: bytes | None) -> tuple[int, int] | None:
    if not data:
        return None
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.ReadOnly)
    sz = QImageReader(buf).size()
    return (sz.width(), sz.height()) if sz.isValid() and sz.width() > 0 else None


def build_chapter(book: Book, d: int, px: int, col_w: float, page_h: float | None, dpr: float,
                  chapter_gap: bool = False, style: BookStyle = BookStyle()) -> _ChapterDoc:
    """把第 d 个正文文件排成一个文档。page_h=None：不分页（滚动用）。"""
    doc = _ChapterDoc(book, dpr)
    doc.setUndoRedoEnabled(False)
    doc.setDocumentMargin(0)
    body = theme.book_font(px, style.face, style.weight)
    doc.setDefaultFont(body)
    natural = theme.natural_line(body)          # Qt 的比例行高乘在字体的自然行距上，换字体要重算
    # 正文按像素定死行距：比例行高乘在「这一行里最高的字」上，楷体、回退字体的数字、上标会把个别行撑高
    line_px = round(px * style.line, 2)
    fixed = QTextBlockFormat.LineHeightTypes.FixedHeight.value
    head_lh = HEAD_LINE / natural * 100
    sup = QTextCharFormat()
    sup.setVerticalAlignment(QTextCharFormat.AlignSuperScript)
    thin = QTextCharFormat()
    thin.setFontLetterSpacingType(QFont.AbsoluteSpacing)
    thin.setFontLetterSpacing(-px * 0.2)
    # 中文里的弯引号要占一个字宽（大陆的规矩）。MiSans 的 “” 是西文那种窄的，「“社会主义”或」挤成一团：
    # 字形不换（换字体会把那一行撑高），只在旁边补空 —— 闭引号补在自己后面，开引号补在前一个字后面
    fm = QFontMetricsF(body)
    pads = {}
    for q in "“”‘’":
        extra = px - fm.horizontalAdvance(q)
        if extra > px * 0.2:
            f = QTextCharFormat()
            f.setFontLetterSpacingType(QFont.AbsoluteSpacing)
            f.setFontLetterSpacing(extra)
            pads[q] = f
    prop = QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
    max_img_h = (page_h - 8) if page_h else col_w * 1.6

    cur = QTextCursor(doc)
    for i, blk in enumerate(book.blocks[d]):
        bf = QTextBlockFormat()
        cf = QTextCharFormat()
        kind = blk.kind
        if kind in HEAD_EM:
            cf.setFont(theme.book_font(round(px * HEAD_EM[kind]), style.face, 3))
            bf.setTopMargin(0 if i == 0 else round(px * 1.3))
            bf.setBottomMargin(round(px * 22 / 18))
            bf.setLineHeight(head_lh, prop)
            bf.setAlignment(Qt.AlignLeft)
        elif kind == "img":
            bf.setAlignment(Qt.AlignHCenter)
            bf.setTopMargin(round(px * 0.5))
            bf.setBottomMargin(round(px * 0.85))
        else:
            bf.setTextIndent(2 * px)
            bf.setAlignment(Qt.AlignJustify)
            bf.setBottomMargin(round(px * 0.85, 1))
            bf.setLineHeight(line_px, fixed)
        if i == 0 and chapter_gap:
            bf.setTopMargin(0)
        if i == 0:
            cur.setBlockFormat(bf)
            cur.setBlockCharFormat(cf)
        else:
            cur.insertBlock(bf, cf)
        if kind == "img":
            data = book.image_bytes(blk.src)
            sz = _image_size(data)
            if sz:
                w, h = sz
                s = min(1.0, col_w / w, max_img_h / h)
                name = f"img:{i}:{blk.src}"
                doc.imgs[name] = (blk.src, w * s, h * s)
                f = QTextImageFormat()
                f.setName(name)
                f.setWidth(w * s)
                f.setHeight(h * s)
                cur.insertImage(f)
            else:
                cur.insertText(_OBJ, cf)   # 解不开的图：占一个字，位置照旧
        else:
            text = blk.text.replace(chr(13), _LS).replace(chr(10), _LS).replace(chr(0x2029), _LS)
            text = glue_punct(text.replace(_ZW, " "))
            ins = break_points(text)
            if ins:
                doc.ins[i] = ins
            text = with_breaks(text, ins)
            spans = []      # (起, 止, 格式)：只换字形 / 位置，字还是那几个，块内偏移不变
            if kind == "p":
                # 注释编号「[1]」排成上标，不再像正文一样占着一个字宽打断句子
                spans += [(m.start(), m.end(), sup) for m in _NOTE_REF.finditer(text)]
            if pads:
                for i in cjk_quotes(text):
                    q = text[i]
                    if q not in pads:
                        continue
                    if q in _OPEN_Q:
                        if i > 0 and text[i - 1] not in (_LS, _GLUE):
                            spans.append((i - 1, i, pads[q]))
                    else:
                        spans.append((i, i + 1, pads[q]))
            # 转制留下的空格（「战争 [1] ，」）：压到几乎没有宽度
            spans += [(m.start(), m.end(), thin) for m in re.finditer(_GLUE, text)]
            if not spans:
                cur.insertText(text, cf)
            else:
                last = 0
                for a, b, f in sorted(spans, key=lambda x: x[0]):
                    if a < last:
                        continue
                    cur.insertText(text[last:a], cf)
                    g = QTextCharFormat(cf)
                    g.merge(f)
                    cur.insertText(text[a:b], g)
                    last = b
                cur.insertText(text[last:], cf)
    if page_h:
        doc.setPageSize(QSizeF(col_w, page_h))
    else:
        doc.setTextWidth(col_w)
    return doc


def page_table(doc: QTextDocument, page_h: float) -> list[int]:
    """每页第一行的字符位置。"""
    n = max(1, doc.pageCount())
    starts: list[int | None] = [None] * n
    blk = doc.begin()
    while blk.isValid():
        lay = blk.layout()
        cnt = lay.lineCount()
        if cnt:
            oy = lay.position().y()
            bp = blk.position()
            first = int((oy + lay.lineAt(0).y() + 0.5) // page_h)
            last = int((oy + lay.lineAt(cnt - 1).y() + 0.5) // page_h)
            if first == last:
                if 0 <= first < n and starts[first] is None:
                    starts[first] = bp
            else:
                for i in range(cnt):
                    ln = lay.lineAt(i)
                    pg = int((oy + ln.y() + 0.5) // page_h)
                    if 0 <= pg < n and starts[pg] is None:
                        starts[pg] = bp + ln.textStart()
        blk = blk.next()
    out, last_pos = [], 0
    for s in starts:
        last_pos = last_pos if s is None else s
        out.append(last_pos)
    return out


def range_rects(doc: QTextDocument, a: int, b: int, y0: float | None = None,
                y1: float | None = None) -> list[tuple[QRectF, float]]:
    """[a, b) 在文档里占的每一行的字框（字的上下沿各放 1px）和基线 y。只要 y0..y1 之间的。"""
    out = []
    if b <= a:
        return out
    blk = doc.findBlock(a)
    while blk.isValid() and blk.position() < b:
        bp = blk.position()
        s = max(a, bp) - bp
        e = min(b, bp + blk.length() - 1) - bp
        lay = blk.layout()
        org = lay.position()
        if e > s:
            for i in range(lay.lineCount()):
                ln = lay.lineAt(i)
                ls = ln.textStart()
                le = ls + ln.textLength()
                if le <= s or ls >= e:
                    continue
                top = org.y() + ln.y()
                if y1 is not None and top > y1:
                    return out
                if y0 is not None and top + ln.height() < y0:
                    continue
                x1 = ln.cursorToX(max(s, ls))[0]
                x2 = ln.cursorToX(min(e, le))[0]
                base = top + ln.ascent()
                r = QRectF(org.x() + min(x1, x2), base - ln.ascent() - 1,
                           abs(x2 - x1), ln.ascent() + ln.descent() + 2)
                out.append((r, base))
        blk = blk.next()
    return out


class _Canvas(QWidget):
    def __init__(self, view: "EpubView"):
        super().__init__(view.stage)
        self.v = view
        self.setMouseTracking(False)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.IBeamCursor)

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
        self.v._resized()


class EpubView(BookView):
    BG_KEY = "paper"
    DIM_KEY = "paper"

    def __init__(self, parent=None):
        super().__init__(parent)
        prefs = load_prefs()
        self._px = prefs.get("epub_px", theme.BOOK_PX)
        if self._px not in FONT_SIZES:
            self._px = theme.BOOK_PX
        face = prefs.get("epub_face", "sans")
        self._style = BookStyle(face if face in theme.BOOK_FACES else "sans",
                                prefs.get("epub_weight", 1) if prefs.get("epub_weight") in (0, 1, 2) else 1,
                                LINES.get(prefs.get("epub_line"), theme.BOOK_LINE))
        self._width = prefs.get("epub_width") if prefs.get("epub_width") in WIDTHS else "normal"
        self._mode = prefs.get("epub_mode", "paged")
        if self._mode not in ("paged", "scroll"):
            self._mode = "paged"
        self._spread = bool(prefs.get("epub_spread", False))   # 要不要双页
        self._two = False              # 现在是不是真的两页并排（要双页、分页、窗口够宽）
        self._anchor: tuple[int, int] | None = None   # 读到这儿的字：(章, 字符位置)，见文件开头
        self._old: dict[int, float] = {}              # 重排前每章的页数（估还没排的章用）
        self._old_cpp = 1.0                           # 重排前按版面算的一页多少字
        self._est: tuple | None = None                # (每页字数, 页数比例) 的缓存，排好一章就作废
        self._chs: list[_Chapter] = []
        self._nonempty: list[int] = []
        self._geom: tuple | None = None
        self._col_w = 612.0
        self._col_x = 0.0
        self._page_h = 600.0
        self._cur = (0, 0)             # 分页：(章, 章内页)
        self._sd, self._soff = 0, 0.0  # 滚动：(章, 章内偏移)
        self._drawn: list[tuple[int, float, float]] = []
        self._sel: tuple[int, int, int] | None = None     # (章, 起点, 当前点)
        self._press_at: QPointF | None = None
        self._pop_target: tuple | None = None
        self._src: tuple[int, int, int] | None = None
        self._asked: tuple[int, int, int] | None = None
        self._hl_cache: dict[int, list[tuple[int, int, dict]]] = {}
        self._wheel_acc = 0
        self._wheel_last = 0.0
        self._clicks = 0
        self._click_t = 0.0
        self._painted = False

        self.canvas = _Canvas(self)
        self.sbar = QScrollBar(Qt.Vertical, self.stage)
        self.sbar.setFocusPolicy(Qt.NoFocus)
        self.sbar.hide()
        self.sbar.valueChanged.connect(self._sbar_moved)
        self._sbar_guard = False
        self.stage.resized.connect(self._fit_canvas)

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(1)
        self._idle.timeout.connect(self._idle_step)
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(120)
        self._relayout_timer.timeout.connect(lambda: self._relayout())
        self.bar.aa.setToolTip("字号、单页 / 双页 / 滚动")
        self.canvas.lower()

    # ── 接口 ──────────────────────────────────────────────
    def open(self, book: Book, loc: Loc | None, highlights: list[dict]) -> None:
        self.book = book
        self._hls = [dict(h) for h in highlights]
        self._hl_cache.clear()
        self._chs = [_Chapter(d, sum(len(b.text) for b in blocks if b.kind != "img"))
                     for d, blocks in enumerate(book.blocks)]
        self._nonempty = [d for d, blocks in enumerate(book.blocks) if blocks]
        self._sel = self._src = self._asked = None
        self.pill.hide()
        self.hint.hide()
        self.popover.hide()
        self._barrier = None
        self._last_loc = None
        self._geom = None
        self._anchor = None
        self._old = {}
        self._est = None
        start = loc if loc is not None else Loc(self._nonempty[0] if self._nonempty else 0, 0)
        self._fit_canvas()
        self._relayout(anchor=self._pos_of(start) if self._nonempty else (0, 0))
        self._emit_location()

    def goto(self, loc: Loc) -> None:
        if not self._ready():
            return
        d, pos = self._pos_of(loc)
        self._goto_pos(d, pos)

    def current_loc(self) -> Loc:
        """屏幕上的第一段。分页时优先取「读到这儿」的那段（跳过来的那段、翻页时这一页上第一个开头的段）：
        这样 goto(current_loc()) 回到的正是这一页，page_label 也跟底栏的页码一致，换字号也不会变；
        整页都是上一页接过来的同一段时，就取那一段。"""
        if not self._ready():
            return Loc(0, 0)
        if self._mode == "paged":
            d, p = self._cur
            ch = self._ensure_paged(d)
            start, end = self._page_span(d, p)
            a = self._anchor
            if a is not None and a[0] == d and start <= a[1] < end:
                blk = ch.doc.findBlock(a[1])
                if blk.position() >= start:
                    return Loc(d, blk.blockNumber())
            return self._loc_of(d, self._page_anchor(d, p))
        return self._loc_of(*self._top_pos())

    def page_label(self, loc: Loc) -> str:
        if not self._ready():
            return ""
        d, pos = self._pos_of(loc)
        return f"第 {self._global_page(d, self._page_of(d, pos)) + 1} 页"

    def mark_source(self, loc: Loc, quote: str) -> None:
        if not self._ready():
            return
        self.popover.hide()
        before = self._snapshot()
        before_label = self._label_here()
        hit = self._find_quote(loc, quote)
        self._src = hit
        if hit:
            self._goto_pos(hit[0], hit[1], third=True)
        elif loc is not None:
            self.goto(loc)
        here = self._label_here()
        self._return_to = before
        self._src_label = here
        self.pill.show_jump(here, before_label if before_label != here else None)
        self.canvas.update()

    def _clear_source_mark(self) -> None:
        self._src = None

    def page_forward(self) -> None:
        if not self._ready():
            return
        self.popover.hide()
        if self._mode == "scroll":
            self._scroll_by(self._view_h() - self._line_h())
            return
        d, p = self._cur
        ch = self._ensure_paged(d)
        step = self._step()
        if p + step < len(ch.pages):
            nd, np_, start = d, p + step, ch.pages[p + step]
        else:
            nd = self._next_ch(d)
            if nd is None:
                return
            np_, start = 0, 0
        if not self._allowed(nd, start) and self._blocked():
            return
        self._show_page(nd, np_)

    def page_back(self) -> None:
        if not self._ready():
            return
        self.popover.hide()
        self.hint.hide()
        if self._mode == "scroll":
            self._scroll_by(-(self._view_h() - self._line_h()))
            return
        d, p = self._cur
        if p > 0:
            self._show_page(d, max(0, p - self._step()))
            return
        pd = self._prev_ch(d)
        if pd is not None:
            self._show_page(pd, len(self._ensure_paged(pd).pages) - 1)

    def set_font_px(self, px: int) -> None:
        if px == self._px or px not in FONT_SIZES:
            return
        self._px = px
        save_prefs(epub_px=px)
        self._relayout(force=True)

    def set_face(self, face: str) -> None:
        """字体：sans 黑体 / serif 宋体 / kai 楷体（这台电脑上没有的不给选）。"""
        if face == self._style.face or face not in theme.BOOK_FACES:
            return
        self._style = replace(self._style, face=face)
        save_prefs(epub_face=face)
        self._relayout(force=True)

    def set_weight(self, level: int) -> None:
        """粗细：0 细、1 标准、2 粗。"""
        if level == self._style.weight or level not in (0, 1, 2):
            return
        self._style = replace(self._style, weight=level)
        save_prefs(epub_weight=level)
        self._relayout(force=True)

    def set_line(self, key: str) -> None:
        """行距：tight 紧 / normal 适中 / loose 松。"""
        if key not in LINES or LINES[key] == self._style.line:
            return
        self._style = replace(self._style, line=LINES[key])
        save_prefs(epub_line=key)
        self._relayout(force=True)

    def set_width(self, key: str) -> None:
        """一行最多排多少字：narrow 窄 / normal 适中 / wide 宽（窗口不够宽时按窗口来）。"""
        if key == self._width or key not in WIDTHS:
            return
        self._width = key
        save_prefs(epub_width=key)
        self._relayout(force=True)

    def reset_style(self) -> None:
        """Aa 里的「恢复默认」：字号、字体、粗细、行距、每行字数回到界面稿的样子（单页 / 双页 / 滚动不动）。"""
        self._px, self._style, self._width = theme.BOOK_PX, BookStyle(), "normal"
        save_prefs(epub_px=self._px, epub_face="sans", epub_weight=1, epub_line="normal", epub_width="normal")
        self._relayout(force=True)

    def style(self) -> BookStyle:
        return self._style

    def line_key(self) -> str:
        return next((k for k, v in LINES.items() if v == self._style.line), "normal")

    def width_key(self) -> str:
        return self._width

    def zoom(self, d: int) -> None:
        """Ctrl + 滚轮、Ctrl + = / -：字号大一档 / 小一档。"""
        sizes = FONT_SIZES if d > 0 else FONT_SIZES[::-1]
        nxt = next((s for s in sizes if (s > self._px if d > 0 else s < self._px)), None)
        if nxt is not None:
            self._act()
            self.set_font_px(nxt)

    def set_mode(self, mode: str) -> None:
        if mode == self._mode or mode not in ("paged", "scroll"):
            return
        anchor = self._top_pos() if self._ready() else None
        self._mode = mode
        save_prefs(epub_mode=mode)
        self._sel = None
        self.popover.hide()
        self.sbar.setVisible(mode == "scroll")
        self._fit_canvas()
        if anchor:
            self._relayout(anchor=anchor)     # 双页时分页和滚动的栏宽不一样，要重排

    def set_spread(self, on: bool) -> None:
        """分页时左右两页并排（窗口不够宽就先单页）。"""
        on = bool(on)
        if on == self._spread:
            return
        self._spread = on
        save_prefs(epub_spread=on)
        self._sel = None
        self.popover.hide()
        if self._ready() and self._mode == "paged":
            self._relayout(anchor=self._anchor or self._top_pos())

    def set_display(self, key: str) -> None:
        """Aa 里「显示」那一行：paged 单页 / spread 双页 / scroll 滚动。"""
        if key == "scroll":
            self.set_mode("scroll")
            return
        self.set_spread(key == "spread")    # 滚动时只记下来，切回分页时一起排
        self.set_mode("paged")

    def display(self) -> str:
        return "scroll" if self._mode == "scroll" else ("spread" if self._spread else "paged")

    def two_pages(self) -> bool:
        """现在是不是真的两页并排。"""
        return self._two and self._mode == "paged"

    def mode(self) -> str:
        return self._mode

    def font_px(self) -> int:
        return self._px

    def set_barrier(self, section_idx: int | None) -> None:
        super().set_barrier(section_idx)
        self.update_view()

    # ── 位置 ──────────────────────────────────────────────
    def _ready(self) -> bool:
        return self.book is not None and bool(self._nonempty)

    def _next_ch(self, d: int) -> int | None:
        i = bisect_right(self._nonempty, d)
        return self._nonempty[i] if i < len(self._nonempty) else None

    def _prev_ch(self, d: int) -> int | None:
        i = bisect_right(self._nonempty, d - 1) - 1
        return self._nonempty[i] if i >= 0 and self._nonempty[i] < d else None

    def _pos_of(self, loc: Loc) -> tuple[int, int]:
        """Loc → (章, 章里的字符位置)。空的章、超出范围的都落到最近的有内容的地方。"""
        d = loc.doc
        if d >= len(self._chs):
            d = self._nonempty[-1]
            doc = self._ensure_paged(d).doc
            return d, max(0, doc.characterCount() - 1)
        if d < 0:
            return self._nonempty[0], 0
        if not self.book.blocks[d]:
            nd = self._next_ch(d)
            if nd is None:
                return self._pos_of(Loc(len(self._chs), 0))
            return nd, 0
        doc = self._ensure_paged(d).doc
        if loc.block >= doc.blockCount():
            return d, max(0, doc.characterCount() - 1)
        return d, doc.findBlockByNumber(max(0, loc.block)).position()

    def _loc_of(self, d: int, pos: int) -> Loc:
        doc = self._ensure_paged(d).doc
        return Loc(d, doc.findBlock(max(0, pos)).blockNumber())

    def _page_of(self, d: int, pos: int) -> int:
        pages = self._ensure_paged(d).pages
        return max(0, bisect_right(pages, pos) - 1)

    def _step(self) -> int:
        """翻一下走几页：双页时两页。"""
        return 2 if self._two else 1

    def _pages_here(self, d: int, p: int) -> int:
        """从第 p 页起画几页：单页 1；双页 2，章末单出来一页、（挡板模式下）右页过了挡板时 1。"""
        if not self._two:
            return 1
        pages = self._ensure_paged(d).pages
        return 2 if p + 1 < len(pages) and (not self.BLOCK or self._allowed(d, pages[p + 1])) else 1

    def _page_span(self, d: int, p: int) -> tuple[int, int]:
        """屏幕上看得到的字：[这一页（双页时左页）的第一个字, 画出来的最后一页后面那页的第一个字)。"""
        ch = self._ensure_paged(d)
        nxt = p + self._pages_here(d, p)
        end = ch.pages[nxt] if nxt < len(ch.pages) else ch.doc.characterCount()
        return ch.pages[min(p, len(ch.pages) - 1)], end

    def _page_anchor(self, d: int, p: int) -> int:
        """翻到这一页时「读到这儿」的字：这一页上第一个开头的段；整页都是上一页接过来的那段，就是页首。"""
        ch = self._ensure_paged(d)
        start, end = self._page_span(d, p)
        blk = ch.doc.findBlock(start)
        if blk.position() < start:
            nxt = blk.next()
            if nxt.isValid() and nxt.position() < end:
                return nxt.position()
        return start

    def _estimates(self) -> tuple[float, float | None]:
        """(每页大约多少字, 这次重排后页数是重排前的几倍)。用排好的章估没排的章。"""
        if self._est is not None:
            return self._est
        chars = pages = 0
        new = old = 0.0
        for d, ch in enumerate(self._chs):
            if ch.doc is None:
                continue
            if ch.chars:
                chars += ch.chars
                pages += len(ch.pages)
            if d in self._old:
                new += len(ch.pages)
                old += self._old[d]
        if self._old:
            # 刚重排时只排好了一两章，比例不稳：先按「一页排得下多少字」的变化估，排好的章越多越信实测
            prior = self._old_cpp / self._theory_cpp()
            new, old = new + prior * RATIO_PRIOR, old + RATIO_PRIOR
        if pages:
            cpp = max(50.0, chars / pages)
        else:
            per_line = self._col_w / self._px
            lines = self._page_h / (self._px * self._style.line)
            cpp = max(50.0, per_line * lines * 0.8)
        self._est = (cpp, new / old if old else None)
        return self._est

    def _theory_cpp(self, geom: tuple | None = None) -> float:
        """按版面（字号、栏宽、页高）算的一页大约多少字，不看书的内容。"""
        px, col_w, page_h, _dpr, _two, line = (geom or self._geom)[:6]
        lines = max(1, int(page_h // (px * line)))
        return max(1.0, col_w / px * lines)

    def _cpp(self) -> float:
        return self._estimates()[0]

    def _pages_est(self, d: int) -> float:
        """还没排的章大约几页：有重排前的页数就按比例换算（章末半页、图、标题都算进去了，比按字数估准）。"""
        cpp, ratio = self._estimates()
        if ratio is not None and d in self._old:
            return max(1.0, self._old[d] * ratio)
        return max(1.0, self._chs[d].chars / cpp)

    def _pages_in(self, d: int) -> tuple[int, bool]:
        ch = self._chs[d]
        if ch.doc is not None:
            return len(ch.pages), True
        return max(1, round(self._pages_est(d))), False

    def _global_page(self, d: int, p: int) -> int:
        n = 0
        for od in self._nonempty:
            if od >= d:
                break
            n += self._pages_in(od)[0]
        return n + p

    def _total_pages(self) -> tuple[int, bool]:
        tot, exact = 0, True
        for d in self._nonempty:
            n, ok = self._pages_in(d)
            tot += n
            exact = exact and ok
        return tot, exact

    def _top_pos(self) -> tuple[int, int]:
        """屏幕上第一行的位置。"""
        if self._mode == "paged":
            d, p = self._cur
            pages = self._ensure_paged(d).pages
            return d, pages[min(p, len(pages) - 1)]
        d, off = self._sd, self._soff
        ch = self._ensure_scroll(d)
        if off >= ch.sh:
            nd = self._next_ch(d)
            if nd is not None:
                return nd, 0
        pos = ch.sdoc.documentLayout().hitTest(QPointF(1, max(0.0, off) + 2), Qt.FuzzyHit)
        return d, max(0, pos)

    def _allowed(self, d: int, pos: int) -> bool:
        b = self._barrier_pos()
        return b is None or (d, pos) < b

    def _barrier_pos(self) -> tuple[int, int] | None:
        bl = self._barrier_loc()
        return None if bl is None else self._pos_of(bl)

    def _snapshot(self) -> tuple:
        if self._mode == "paged":
            return ("paged",) + self._cur + self._top_pos() + (self._anchor,)
        return ("scroll", self._sd, self._soff) + self._top_pos() + (self._anchor,)

    def _label_here(self) -> str:
        d, pos = self._top_pos()
        return f"第 {self._global_page(d, self._page_of(d, pos)) + 1} 页"

    def _go_back(self) -> None:
        snap = self._return_to
        self.pill.hide()
        self._return_to = None
        self._act()
        if not snap:
            return
        anchor = snap[5]
        if anchor is not None and snap[0] == self._mode == "paged":
            self._goto_pos(*anchor)           # 中间换过字号也回得去
        elif snap[0] == self._mode == "scroll":
            self._sd, self._soff = snap[1], snap[2]
            self._after_scroll()
        else:
            self._goto_pos(snap[3], snap[4])

    # ── 排版 ──────────────────────────────────────────────
    def _dpr(self) -> float:
        return self.canvas.devicePixelRatioF() or 1.0

    def _view_h(self) -> float:
        return max(100.0, self.canvas.height() - PAD_TOP - PAD_BOTTOM)

    def _scroll_vis(self) -> float:
        """滚动时从章里的 soff 往下能看到多高（字画到画布底边）。"""
        return float(max(100, self.canvas.height() - PAD_TOP))

    def _line_h(self) -> float:
        return self._px * self._style.line

    def _gap(self) -> float:
        return self._px * CH_GAP_EM

    def _fit_canvas(self) -> None:
        r = self.stage.rect()
        self.canvas.setGeometry(r)
        sw = 10
        self.sbar.setGeometry(r.width() - sw - 2, 2, sw, r.height() - 4)
        self.sbar.setVisible(self._mode == "scroll" and self.book is not None)
        self.sbar.raise_()
        self.pill.raise_()
        self.hint.raise_()
        self.popover.raise_()
        self.dim.raise_()

    def _resized(self) -> None:
        self.popover.hide()
        if self.book is None:
            return
        if self._geom is None or not self._painted:
            self._relayout()        # 还没画过（刚显示出来）：马上按真的大小排，不要先闪一下旧的
        else:
            self._relayout_timer.start()

    def _compute_geom(self) -> tuple:
        """(字号, 栏宽, 页高, 设备像素比, 是不是两页并排, 行距, 样式)。"""
        w, h = self.canvas.width(), self.canvas.height()
        px = self._px
        full = round(WIDTHS[self._width] * px)
        side = max(PAD_SIDE, round(px * 1.5))      # 字很大时两边也多留一点，不贴着窗口边
        two = False
        col_w = float(min(full, max(200, w - 2 * side)))
        if self._spread and self._mode == "paged":
            half = (w - 2 * side - GUTTER) // 2
            if half >= SPREAD_MIN_EM * px:
                two = True
                col_w = float(min(full, half))
        # 栏宽取字号的整数倍（clreq「行长应为文字尺寸的整数倍」）：整行汉字两端对齐时字距不会被拉开
        col_w = float(max(px * 4, col_w // px * px))
        page_h = float(max(120, h - PAD_TOP - PAD_BOTTOM))
        return (self._px, col_w, page_h, round(self._dpr(), 3), two, self._style.line, self._style)

    def _set_geom(self, geom: tuple) -> None:
        self._geom = geom
        _, self._col_w, self._page_h, _, self._two = geom[:5]
        self._col_x = round((self.canvas.width() - self._spread_w()) / 2)

    def _spread_w(self) -> float:
        """书页上字占的总宽度：单页一栏，双页两栏加中缝。"""
        return 2 * self._col_w + GUTTER if self._two else self._col_w

    def _relayout(self, anchor: tuple[int, int] | None = None, force: bool = False) -> None:
        if self.book is None or not self._nonempty:
            return
        geom = self._compute_geom()
        if geom == self._geom and not force and anchor is None:
            self._set_geom(geom)      # 只是窗口宽了一点：栏居中
            self.canvas.update()
            return
        if anchor is None and self._geom is not None:
            anchor = self._anchor or self._top_pos()
        if geom != self._geom or force:
            # 重排前的页数留着：没排到的章按比例估页码（见 _pages_est）
            if self._geom is not None:
                self._old = {d: float(len(self._chs[d].pages)) if self._chs[d].doc is not None
                             else self._pages_est(d) for d in self._nonempty}
                self._old_cpp = self._theory_cpp(self._geom)    # 用重排前的版面（_px 这时已经是新的了）
            for ch in self._chs:
                ch.doc = None
                ch.pages = []
                ch.sdoc = None
            self._est = None
            self._last_loc = None     # 位置（段）没变页码也变了：再报一次，让顶条、左栏的页码跟上
        self._set_geom(geom)
        self._goto_pos(*(anchor or (self._nonempty[0], 0)))
        self._idle.start()

    def _ensure_paged(self, d: int) -> _Chapter:
        ch = self._chs[d]
        if ch.doc is None:
            if self._geom is None:
                self._set_geom(self._compute_geom())
            ch.doc = build_chapter(self.book, d, self._px, self._col_w, self._page_h, self._dpr(), style=self._style)
            ch.pages = page_table(ch.doc, self._page_h)
            self._hl_cache.pop(d, None)
            self._est = None
        return ch

    def _ensure_scroll(self, d: int) -> _Chapter:
        ch = self._chs[d]
        if ch.sdoc is None:
            if self._geom is None:
                self._ensure_paged(d)
            ch.sdoc = build_chapter(self.book, d, self._px, self._col_w, None, self._dpr(), style=self._style)
            ch.sh = ch.sdoc.documentLayout().documentSize().height()
        return ch

    def _idle_step(self) -> None:
        """空闲时排下一章：当前这章前面的先排（排完当前的页码就准了），再按离当前远近排后面的。"""
        if self.book is None:
            return
        cur = self._cur[0] if self._mode == "paged" else self._sd
        todo = [d for d in self._nonempty if self._chs[d].doc is None]
        if not todo:
            self._update_bar()
            return
        before = [d for d in todo if d < cur]
        d = max(before) if before else min(todo, key=lambda x: abs(x - cur))
        self._ensure_paged(d)
        if len(before) == 1 and len(todo) > 1:
            # 前面的章都排好了：当前页码准了（总页数还是估的）。再报一次位置，顶条、左栏的页码跟着改
            self._update_bar()
            self._last_loc = None
            self._emit_location()
        if len(todo) > 1:
            self._idle.start()
        else:
            # 全排完了：页码准了。位置没变也再发一次 location_changed，让用页码的地方（AI 栏顶上）刷新
            self._update_bar()
            self._last_loc = None
            self._emit_location()

    def layout_done(self) -> bool:
        return all(self._chs[d].doc is not None for d in self._nonempty)

    # ── 走到某处 ───────────────────────────────────────────
    def _goto_pos(self, d: int, pos: int, third: bool = False) -> None:
        if self._mode == "paged":
            self._ensure_paged(d)
            self._show_page(d, self._page_of(d, pos), anchor=pos)
        else:
            ch = self._ensure_scroll(d)
            y = self._line_top(ch.sdoc, pos)
            if third:
                y -= self._view_h() / 3
            self._sd, self._soff = d, y
            self._after_scroll(clamp_only_end=True, anchor=None if third else (d, pos))

    def _show_page(self, d: int, p: int, anchor: int | None = None) -> None:
        """翻到第 d 章第 p 页（双页时落到这一对的左页）。anchor：跳过来的那个字，不给就按这一页算。"""
        if self._two:
            p -= p % 2
        self._cur = (d, p)
        self._anchor = (d, anchor if anchor is not None else self._page_anchor(d, p))
        self._sel = None
        self.popover.hide()
        if self.hint.isVisible():
            ch = self._ensure_paged(d)
            step = self._step()
            nxt = ch.pages[p + step] if p + step < len(ch.pages) else None
            nd = d if nxt is not None else self._next_ch(d)
            if nd is None or self._allowed(nd, nxt or 0):
                self.hint.hide()
        self.canvas.update()
        self._update_bar()
        self._emit_location()

    @staticmethod
    def _line_top(doc: QTextDocument, pos: int) -> float:
        blk = doc.findBlock(pos)
        lay = blk.layout()
        if lay.lineCount() == 0:
            return lay.position().y()
        ln = lay.lineForTextPosition(max(0, pos - blk.position()))
        if not ln.isValid():
            ln = lay.lineAt(0)
        return lay.position().y() + ln.y()

    # ── 滚动 ──────────────────────────────────────────────
    def _norm_scroll(self, d: int, off: float) -> tuple[int, float]:
        gap = self._gap()
        while True:
            h = self._ensure_scroll(d).sh
            if off >= h + gap:
                nd = self._next_ch(d)
                if nd is None:
                    break
                off -= h + gap
                d = nd
                continue
            if off < 0:
                pd = self._prev_ch(d)
                if pd is None:
                    off = 0.0
                    break
                d = pd
                off += self._ensure_scroll(pd).sh + gap
                continue
            break
        if self._next_ch(d) is None:
            off = min(off, max(0.0, self._chs[d].sh - self._view_h() + self._line_h()))
        return d, max(0.0, off)

    def _scroll_by(self, dy: float) -> None:
        if dy == 0:
            return
        if dy < 0:
            self.hint.hide()
        old = (self._sd, self._soff)
        d, off = self._norm_scroll(self._sd, self._soff + dy)
        if dy > 0:
            b = self._barrier_pos()
            if b is not None:
                bd, bpos = b
                by = self._line_top(self._ensure_scroll(bd).sdoc, bpos)
                # 屏幕最下面（滚动时字一直画到底边）不能越过挡板那一行的上沿
                vis = self._scroll_vis()
                bot = self._norm_forward(d, off + vis)
                if bot > (bd, by) and self._blocked():
                    tgt = self._norm_scroll(bd, by - vis)
                    d, off = max(tgt, old)
        if (d, off) != old:
            self._sd, self._soff = d, off
            self._after_scroll()

    def _norm_forward(self, d: int, off: float) -> tuple[int, float]:
        gap = self._gap()
        while True:
            h = self._ensure_scroll(d).sh
            if off < h + gap:
                return d, off
            nd = self._next_ch(d)
            if nd is None:
                return d, off
            off -= h + gap
            d = nd

    def _after_scroll(self, clamp_only_end: bool = False, anchor: tuple[int, int] | None = None) -> None:
        self._sd, self._soff = self._norm_scroll(self._sd, self._soff)
        self._anchor = anchor or self._top_pos()
        self._sel = None
        self.popover.hide()
        ch = self._chs[self._sd]
        self._sbar_guard = True
        self.sbar.setRange(0, max(0, int(ch.sh + self._gap() - self._view_h())))
        self.sbar.setPageStep(int(self._view_h()))
        self.sbar.setSingleStep(int(self._line_h()))
        self.sbar.setValue(int(self._soff))
        self._sbar_guard = False
        self.canvas.update()
        self._update_bar()
        self._emit_location()

    def _sbar_moved(self, v: int) -> None:
        if self._sbar_guard or self._mode != "scroll":
            return
        self._act()
        self._continuous_input(self._scroll_by, v - self._soff)

    def _scroll_lines(self, n: int) -> None:
        if self._mode == "scroll":
            self._scroll_by(n * self._line_h())
        elif n > 0:
            self.page_forward()
        else:
            self.page_back()

    def _wheel(self, e) -> None:
        self._act()
        self.popover.hide()
        if e.modifiers() & Qt.ControlModifier:        # Ctrl + 滚轮：字号（跟浏览器、Word 一样）
            self._zoom_acc = getattr(self, "_zoom_acc", 0) + e.angleDelta().y()
            if abs(self._zoom_acc) >= 120:
                self.zoom(1 if self._zoom_acc > 0 else -1)
                self._zoom_acc = 0
            return
        if self._mode == "scroll":
            pd = e.pixelDelta().y()
            dy = -pd if pd else -e.angleDelta().y() / 120 * self._line_h() * 3
            self._continuous_input(self._scroll_by, dy)
            return
        self._wheel_acc += e.angleDelta().y()
        now = time.monotonic()
        if abs(self._wheel_acc) >= 120 and now - self._wheel_last > 0.18:
            self._wheel_last = now
            fwd = self._wheel_acc < 0
            self._wheel_acc = 0
            self._continuous_input(self.page_forward if fwd else self.page_back)
        elif abs(self._wheel_acc) >= 120:
            self._wheel_acc = 0

    # ── 画 ────────────────────────────────────────────────
    def _paint(self, p: QPainter, w: int, h: int) -> None:
        p.fillRect(0, 0, w, h, theme.color("paper"))
        if not self._ready():
            return
        self._painted = True
        pal = QPalette()
        pal.setColor(QPalette.Text, theme.color("ink"))
        if self._mode == "paged":
            d, pg = self._cur
            ch = self._ensure_paged(d)
            ph = self._page_h
            side = GUTTER / 2 if self._two else PAD_SIDE   # 双页时两栏各画到中缝为止
            for k in self._shown_pages():
                q = pg + k
                clip = QRectF(0, q * ph, self._col_w, ph)
                p.save()
                p.translate(self._col_x + k * (self._col_w + GUTTER), PAD_TOP - q * ph)
                p.setClipRect(QRectF(-side, q * ph, self._col_w + 2 * side, ph))
                self._paint_doc(p, d, ch.doc, clip, pal)
                p.restore()
            if self._two:
                # 书脊：中缝一条细线
                x = self._col_x + self._col_w + GUTTER / 2
                p.fillRect(QRectF(round(x), PAD_TOP, 1, ph), theme.color("l2"))
        else:
            self._drawn = []
            d = self._sd
            y = PAD_TOP - self._soff
            while d is not None and y < h:
                ch = self._ensure_scroll(d)
                if y + ch.sh > 0:
                    p.save()
                    p.translate(self._col_x, y)
                    self._paint_doc(p, d, ch.sdoc, QRectF(-PAD_SIDE, -y, self._col_w + 2 * PAD_SIDE, h), pal)
                    p.restore()
                self._drawn.append((d, y, ch.sh))
                y += ch.sh + self._gap()
                d = self._next_ch(d)

    def _paint_doc(self, p: QPainter, d: int, doc: QTextDocument, clip: QRectF, pal: QPalette) -> None:
        y0, y1 = clip.top(), clip.bottom()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        # 高亮（黄）
        hl = theme.color("hl")
        for a, b, _h in self._hl_ranges(d):
            for r, _ in range_rects(doc, a, b, y0, y1):
                p.setBrush(hl)
                p.drawRoundedRect(r, 2, 2)
        # 出处（陶土色底 + 2px 下划线）
        if self._src and self._src[0] == d:
            src, acc = theme.color("src"), theme.color("acc")
            for r, _ in range_rects(doc, self._src[1], self._src[2], y0, y1):
                p.setBrush(src)
                p.drawRoundedRect(r, 2, 2)
                p.fillRect(QRectF(r.left(), r.bottom() - 2, r.width(), 2), acc)
        # 选区（蓝灰）
        if self._sel and self._sel[0] == d and self._sel[1] != self._sel[2]:
            a, b = sorted(self._sel[1:])
            p.setBrush(theme.color("sel"))
            for r, _ in range_rects(doc, a, b, y0, y1):
                p.drawRoundedRect(r, 2, 2)
        p.setRenderHint(QPainter.Antialiasing, False)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.clip = clip
        ctx.palette = pal
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        doc.documentLayout().draw(p, ctx)
        # 问过 AI 的那段：细虚线
        if self._asked and self._asked[0] == d:
            pen = QPen(theme.color("acc"), 1.5)
            pen.setDashPattern([1.0, 2.0])
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setRenderHint(QPainter.Antialiasing, True)
            for r, base in range_rects(doc, self._asked[1], self._asked[2], y0, y1):
                y = base + 5
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

    def _hl_ranges(self, d: int) -> list[tuple[int, int, dict]]:
        got = self._hl_cache.get(d)
        if got is not None:
            return got
        out = []
        mine = [h for h in self._hls if h.get("doc") == d]
        if mine:
            doc = self._ensure_paged(d).doc
            for h in mine:
                blk = doc.findBlockByNumber(int(h.get("block", 0)))
                if not blk.isValid():
                    continue
                nb = int(h.get("block", 0))
                n = blk.length() - 1
                s = self._b2d(d, nb, int(h.get("start", 0)))
                e = self._b2d(d, nb, int(h.get("end", 0)), end=True)
                s, e = max(0, min(s, n)), max(0, min(e, n))
                if e > s:
                    out.append((blk.position() + s, blk.position() + e, h))
        self._hl_cache[d] = out
        return out

    def _shown_pages(self) -> list[int]:
        """分页时画出来的是 _cur 往后第几页：单页 [0]；双页 [0, 1]，章末单出来一页、右页过了挡板时只有 [0]。"""
        return list(range(self._pages_here(*self._cur)))

    # ── 鼠标：选字、点高亮 ──────────────────────────────────
    def _hit(self, pt: QPointF, clamp_d: int | None = None) -> tuple[int, int] | None:
        if self._mode == "paged":
            d, pg = self._cur
            doc = self._ensure_paged(d).doc
            k = 0
            if self._two and pt.x() >= self._col_x + self._col_w + GUTTER / 2 and 1 in self._shown_pages():
                k = 1                  # 点在右页
            q = pg + k
            x = min(max(pt.x() - self._col_x - k * (self._col_w + GUTTER), 0.0), self._col_w - 0.01)
            y = pt.y() - PAD_TOP + q * self._page_h
            y = min(max(y, q * self._page_h), (q + 1) * self._page_h - 1)
            return d, doc.documentLayout().hitTest(QPointF(x, y), Qt.FuzzyHit)
        x = min(max(pt.x() - self._col_x, 0.0), self._col_w - 0.01)
        for d, top, h in self._drawn:
            if clamp_d is not None and d != clamp_d:
                continue
            if clamp_d is not None or top - 1 <= pt.y() < top + h + self._gap():
                y = min(max(pt.y() - top, 0.0), h - 1)
                return d, self._chs[d].sdoc.documentLayout().hitTest(QPointF(x, y), Qt.FuzzyHit)
        return None

    def _to_screen(self, d: int, r: QRectF) -> QRectF:
        if self._mode == "paged":
            pg = self._cur[1]
            k = 1 if self._two and int(r.center().y() // self._page_h) == pg + 1 else 0   # 在右页
            return r.translated(self._col_x + k * (self._col_w + GUTTER), PAD_TOP - (pg + k) * self._page_h)
        for dd, top, _h in self._drawn:
            if dd == d:
                return r.translated(self._col_x, top)
        return r

    def _doc(self, d: int) -> QTextDocument:
        return self._ensure_paged(d).doc if self._mode == "paged" else self._ensure_scroll(d).sdoc

    def _press(self, e) -> None:
        self._act()
        self.setFocus(Qt.MouseFocusReason)
        if e.button() != Qt.LeftButton or not self._ready():
            return
        now = time.monotonic()
        near = self._press_at is not None and (e.position() - self._press_at).manhattanLength() < 6
        self._clicks = self._clicks + 1 if (now - self._click_t < 0.45 and near) else 1
        self._click_t = now
        self._press_at = e.position()
        self.popover.hide()
        self._pop_target = None
        if self._clicks >= 3:
            hit = self._hit(e.position())
            if hit:
                blk = self._doc(hit[0]).findBlock(hit[1])
                self._sel = (hit[0], blk.position(), blk.position() + blk.length() - 1)
                self._finish_selection()
            return
        hit = self._hit(e.position())
        self._sel = (hit[0], hit[1], hit[1]) if hit else None
        self.canvas.update()

    def _move(self, e) -> None:
        if not (e.buttons() & Qt.LeftButton) or self._sel is None or self._press_at is None:
            return
        if (e.position() - self._press_at).manhattanLength() < 4:
            return
        hit = self._hit(e.position(), clamp_d=self._sel[0] if self._mode == "scroll" else None)
        if hit and hit[0] == self._sel[0]:
            self._sel = (self._sel[0], self._sel[1], hit[1])
            self.canvas.update()

    def _release(self, e) -> None:
        if e.button() != Qt.LeftButton or self._sel is None:
            return
        d, a, b = self._sel
        if a != b:
            self._finish_selection()
            return
        self._sel = None
        # 单击：点在高亮上 → 小条变「问 AI · 取消高亮」
        for ha, hb, h in self._hl_ranges(d):
            if ha <= a < hb:
                ga, gb = self._group_span(d, h)
                self._show_pop(d, ga, gb, ("hl", h))
                return
        self.canvas.update()

    def _double(self, e) -> None:
        """双击选一句（中文按句读比按词有用）。"""
        self._act()
        if not self._ready():
            return
        hit = self._hit(e.position())
        if not hit:
            return
        d, pos = hit
        blk = self._doc(d).findBlock(pos)
        t = blk.text()
        i = pos - blk.position()
        ends = "。！？!?；;…"
        a = i
        while a > 0 and t[a - 1] not in ends:
            a -= 1
        b = i
        while b < len(t) and t[b] not in ends:
            b += 1
        if b < len(t):
            b += 1
        while a < b and t[a] in " 　" + _LS:
            a += 1
        self._sel = (d, blk.position() + a, blk.position() + b)
        self._clicks = 2
        self._finish_selection()

    def _finish_selection(self) -> None:
        d, a, b = self._sel
        a, b = min(a, b), max(a, b)
        text = self._text(d, a, b)
        if not text.strip():
            self._sel = None
            self.canvas.update()
            return
        self._sel = (d, a, b)
        self._act()
        self._show_pop(d, a, b, ("sel",))

    def _show_pop(self, d: int, a: int, b: int, target: tuple) -> None:
        rects = range_rects(self._doc(d), a, b)
        if not rects:
            return
        vis = [self._to_screen(d, r) for r, _ in rects]
        vis = [r for r in vis if r.bottom() > 0 and r.top() < self.canvas.height()] or vis
        self._pop_target = (d, a, b) + target
        self.popover.show_for(vis[0], vis[-1], existing=target[0] == "hl")
        self.canvas.update()

    def _text(self, d: int, a: int, b: int) -> str:
        c = QTextCursor(self._doc(d))
        c.setPosition(a)
        c.setPosition(b, QTextCursor.KeepAnchor)
        t = c.selectedText()
        t = t.replace(chr(0x2029), chr(10)).replace(_LS, chr(10)).replace(_OBJ, "").replace(_ZW, "")
        return t.replace(_GLUE, " ").strip()

    def _group_span(self, d: int, h: dict) -> tuple[int, int]:
        g = h.get("group")
        rs = [(a, b) for a, b, x in self._hl_ranges(d) if x is h or (g and x.get("group") == g)]
        return min(a for a, _ in rs), max(b for _, b in rs)

    # ── 小条的按钮 ─────────────────────────────────────────
    def _pop_ask(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if not t:
            return
        d, a, b = t[:3]
        text = self._text(d, a, b)
        self._asked = (d, a, b)
        self._sel = None
        self.canvas.update()
        self._act()
        self.ask_ai.emit(text, self._loc_of(d, a))

    def _pop_highlight(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if not t:
            return
        self.add_highlight(*t[:3])
        self._sel = None
        self.canvas.update()

    def _pop_unhighlight(self) -> None:
        t = self._pop_target
        self.popover.hide()
        if not t or t[3] != "hl":
            return
        self.remove_highlight(t[4])

    # 书里的偏移 ↔ 文档里的偏移（文档里有些引号旁边多了零宽空格，见 break_points）
    def _b2d(self, d: int, n: int, off: int, end: bool = False) -> int:
        ins = self._ensure_paged(d).doc.ins.get(n)
        if not ins:
            return off
        return off + (bisect_left(ins, off) if end else bisect_right(ins, off))

    def _d2b(self, d: int, n: int, doff: int) -> int:
        ins = self._ensure_paged(d).doc.ins.get(n)
        if not ins:
            return doff
        k = 0
        for j, x in enumerate(ins):
            if x + j < doff:
                k += 1
            else:
                break
        return doff - k

    def add_highlight(self, d: int, a: int, b: int) -> list[dict]:
        """把章 d 的 [a, b) 标黄；跨段就拆成几条（同一个 group）。跟已有的重叠就并起来。"""
        doc = self._ensure_paged(d).doc
        group = uuid.uuid4().hex[:8]
        new = []
        blk = doc.findBlock(a)
        while blk.isValid() and blk.position() < b:
            bp = blk.position()
            n = blk.blockNumber()
            s = self._d2b(d, n, max(a, bp) - bp)                      # 文档偏移 → 书里的偏移
            e = self._d2b(d, n, min(b, bp + blk.length() - 1) - bp)
            if e > s:
                for h in [h for h in self._hls if h.get("doc") == d and h.get("block") == n]:
                    if h["start"] < e and s < h["end"]:
                        s, e = min(s, h["start"]), max(e, h["end"])
                        self._hls.remove(h)
                text = self.book.blocks[d][n].text[s:e] if n < len(self.book.blocks[d]) else ""
                new.append({"doc": d, "block": n, "start": s, "end": e, "text": text, "group": group})
            blk = blk.next()
        self._hls.extend(new)
        self._hl_cache.pop(d, None)
        self._update_bar()
        self.canvas.update()
        if new:
            self.highlights_changed.emit()
        return new

    def remove_highlight(self, h: dict) -> None:
        g = h.get("group")
        before = len(self._hls)
        self._hls = [x for x in self._hls if not (x is h or x == h or (g and x.get("group") == g))]
        self._hl_cache.clear()
        self._update_bar()
        self.canvas.update()
        if len(self._hls) != before:
            self.highlights_changed.emit()

    def _hl_items(self):
        seen, items = set(), []
        for h in sorted(self._hls, key=lambda x: (x.get("doc", 0), x.get("block", 0), x.get("start", 0))):
            g = h.get("group") or id(h)
            if g in seen:
                continue
            seen.add(g)
            text = "".join(x.get("text", "") for x in self._hls
                           if (x.get("group") or id(x)) == g) if h.get("group") else h.get("text", "")
            text = text.replace("\n", " ")
            text = text[:44] + "…" if len(text) > 45 else text
            label = self.page_label(Loc(h["doc"], h["block"]))
            items.append((f"{label}　{text}", 1, h, True, False, ""))
        return items

    def _goto_highlight(self, h: dict) -> None:
        d = h["doc"]
        for a, _b, x in self._hl_ranges(d):
            if x is h:
                self._goto_pos(d, a)
                return
        self.goto(Loc(d, h.get("block", 0)))

    # ── 出处 ──────────────────────────────────────────────
    def _find_quote(self, loc: Loc | None, quote: str) -> tuple[int, int, int] | None:
        qn, _ = norm_chars(quote or "")
        if len(qn) < 2:
            return None
        if loc is not None and 0 <= loc.doc < len(self._chs) and self.book.blocks[loc.doc]:
            d, b = loc.doc, loc.block
        else:
            d, pos = self._top_pos()
            b = self._loc_of(d, pos).block
        doc = self._ensure_paged(d).doc
        # 先在这一块前后几块里找，再在整章里找；精确找不到才按相似度
        for lo, hi, fuzzy in ((b - 1, b + 8, False), (0, doc.blockCount(), False), (b - 1, b + 8, True),
                              (0, doc.blockCount(), True)):
            s, idx = self._norm_blocks(doc, max(0, lo), min(doc.blockCount(), hi))
            got = find_in(qn, s, idx, fuzzy)
            if got:
                return d, got[0], got[1]
        return None

    @staticmethod
    def _norm_blocks(doc: QTextDocument, lo: int, hi: int) -> tuple[str, list[int]]:
        parts, idx = [], []
        blk = doc.findBlockByNumber(lo)
        while blk.isValid() and blk.blockNumber() < hi:
            s, i = norm_chars(blk.text(), blk.position())
            parts.append(s)
            idx.extend(i)
            blk = blk.next()
        return "".join(parts), idx

    # ── 底栏 ──────────────────────────────────────────────
    def update_view(self) -> None:
        self.canvas.update()
        self._update_bar()

    def _update_bar(self) -> None:
        if not self._ready():
            self.bar.set_page(None, None)
            return
        d, pos = self._top_pos()
        cur = self._global_page(d, self._page_of(d, pos)) + 1
        last = cur + len(self._shown_pages()) - 1 if self._mode == "paged" else cur   # 双页：右页的页码
        tot, exact = self._total_pages()
        self.bar.set_page(cur, max(tot, last), approx=not exact, last=last)
        pct = round(100 * last / max(tot, last))
        here = self.current_loc()
        title = self._chapter_title(here)
        left = self._chapter_left(here, last - 1)
        parts = [title] if title else []
        if left is not None:
            parts.append("本章最后一页" if left <= 0 else f"本章还剩 {left} 页")
        parts.append(f"{pct}%")
        self.bar.set_info(" · ".join(parts))
        self.bar.set_hl_count(len({h.get("group") or id(h) for h in self._hls}))
        self.bar.prev.setEnabled(not (d == self._nonempty[0] and pos == 0 and
                                      (self._mode == "paged" or self._soff <= 0)))

    def keyPressEvent(self, e) -> None:
        if e.modifiers() & Qt.ControlModifier:
            k = e.key()
            if k in (Qt.Key_Equal, Qt.Key_Plus):
                self.zoom(1)
                self._sync_aa()
                return
            if k == Qt.Key_Minus:
                self.zoom(-1)
                self._sync_aa()
                return
        super().keyPressEvent(e)

    def _sync_aa(self) -> None:
        """Aa 面板开着时，用快捷键换了字号：面板上的数跟着变。"""
        st = getattr(self, "_aa_step", None)
        if st is not None and self.aa_panel is not None and self.aa_panel.isVisible():
            try:
                st.cur = self._px
                st._show()
            except RuntimeError:
                pass

    def _chapter_left(self, here: Loc, last: int) -> int | None:
        """这一章（目录里的一项，没有目录按小节）在眼前这页 / 这两页后面还有几页。last：眼前最后一页（从 0 起）。"""
        book = self.book
        nxt = min((e.loc for e in book.toc if e.loc > here), default=None)
        if not book.toc:
            s = book.section_at(here)
            nxt = s.end if s is not None else None
        if nxt is None or nxt >= book.end():
            end = self._total_pages()[0]            # 最后一章：到书尾
        else:
            d, pos = self._pos_of(nxt)
            pg = self._page_of(d, pos)
            pages = self._ensure_paged(d).pages
            # 下一章从一页的开头开始（通常是这样：一章一个文件，从新的一页排起），本章到它前一页为止
            end = self._global_page(d, pg) + (0 if pages[pg] >= pos else 1)
        return end - 1 - last

    def _build_aa(self, panel: AaPanel) -> None:
        faces = theme.book_faces()
        if len(faces) > 1:
            face = Seg([(k, theme.BOOK_FACES[k]) for k in faces], self._style.face)
            tips = {"sans": "MiSans，屏幕上最清楚", "serif": "思源宋体，像纸书", "kai": "楷体，手写的味道"}
            for k, b in face.buttons.items():
                b.setToolTip(tips.get(k, ""))
            face.changed.connect(lambda k: (self._act(), self.set_face(k), _weights()))
            panel.add_row("字体", face)
        st = Stepper(FONT_SIZES, self._px)
        st.setToolTip("也可以按 Ctrl + 滚轮、Ctrl + = / −")
        st.changed.connect(lambda px: (self._act(), self.set_font_px(px)))
        self._aa_step = st
        panel.add_row("字号", st)
        weight = Seg([("0", "细"), ("1", "标准"), ("2", "粗")], str(self._style.weight))

        def _weights() -> None:     # 楷体只有一个字重：没有「细」
            try:
                weight.buttons["0"].setEnabled(self._style.face != "kai")
            except RuntimeError:
                pass
        _weights()
        weight.changed.connect(lambda k: (self._act(), self.set_weight(int(k))))
        panel.add_row("粗细", weight)
        line = Seg([("tight", "紧"), ("normal", "适中"), ("loose", "松")], self.line_key())
        line.changed.connect(lambda k: (self._act(), self.set_line(k)))
        panel.add_row("行距", line)
        width = Seg([("narrow", "窄"), ("normal", "适中"), ("wide", "宽")], self._width)
        for k, n in (("narrow", "每行最多约 28 字"), ("normal", "每行最多约 34 字"), ("wide", "每行最多约 42 字")):
            width.buttons[k].setToolTip(n + "（窗口不够宽时按窗口来）")
        width.changed.connect(lambda k: (self._act(), self.set_width(k)))
        panel.add_row("每行", width)
        seg = Seg([("paged", "单页"), ("spread", "双页"), ("scroll", "滚动")], self.display())
        seg.buttons["spread"].setToolTip("左右两页并排，像摊开的书")
        note = QLabel("窗口不够宽，先按单页显示")
        note.setFont(theme.font(12))
        note.setStyleSheet(theme.qss("color: {t4};"))

        def _pick(key: str) -> None:
            self._act()
            self.set_display(key)
            note.setVisible(key == "spread" and not self.two_pages())
            panel.open_at(self.bar.aa, "right")     # 多了 / 少了一行，重新摆一下
        seg.changed.connect(_pick)
        panel.add_row("显示", seg)
        panel.grid.addWidget(note, panel.grid.rowCount(), 1)
        note.setVisible(self.display() == "spread" and not self.two_pages())
        reset = QPushButton("恢复默认")
        reset.setProperty("kind", "ghost")
        reset.setFocusPolicy(Qt.NoFocus)
        reset.setCursor(Qt.PointingHandCursor)
        reset.setFont(theme.font(12))
        reset.setFixedHeight(26)
        reset.setToolTip("字体、字号、粗细、行距、每行字数回到默认（黑体 18px）")

        def _reset() -> None:
            self._act()
            self.reset_style()
            panel.close_panel()
            self._open_aa()           # 重新建一个面板，上面的选择跟着回到默认
        reset.clicked.connect(_reset)
        panel.grid.addWidget(reset, panel.grid.rowCount(), 1, Qt.AlignLeft)

    def _theme_changed(self) -> None:
        self.canvas.update()
        self.update_view()

    # ── 演示用 ─────────────────────────────────────────────
    def demo_select(self) -> None:
        """在当前页中间选一句，弹出小条（popover 模块的演示和截图用）。"""
        if not self._ready():
            return
        pt = QPointF(self._col_x + self._col_w / 2, PAD_TOP + self._view_h() * 0.45)
        hit = self._hit(pt)
        if not hit:
            return
        ev = type("E", (), {"position": lambda _s: pt})()
        self._double(ev)


if __name__ == "__main__":
    import sys

    from .base import demo_main
    sys.exit(demo_main("epub", "~/Downloads/*.epub", sys.argv[1:]))
