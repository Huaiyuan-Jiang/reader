"""AI 栏里的文字：Markdown → QLabel 能显示的富文本（HTML）。

踩过的坑：
- 段距要明显大于行间空隙：折行和分段一眼分得开（界面稿 `.bot p { margin: 0 0 8px }`）。
- AI 写的单个换行照原样换行（Markdown 本来会把它吞成空格）。用 U+2028（段内换行）接上，
  不用「行尾两个空格」：Qt 会把后者拆成两段，段距跟真分段一样大，又分不清了。
- 加粗最多 600（Qt 的 Markdown 粗体默认 700，中文压成一坨）。
- 行高按 CSS 的意思给（1.78 = 字号的 1.78 倍）。Qt 的「比例行高」乘的是字体自带的行距
  （MiSans 约 1.33 倍字号），所以要换算，不然一行一行松散开。
- 列表拆成「悬挂缩进的段落 + 自己写的项目符号」：QLabel 重新解析 HTML 时列表缩进固定 40px、符号又大。
- 关掉 Markdown 里的 HTML：回答里偶尔有「a<b」这种字，不能被当成标签吃掉。
- 中文的粗体自己认：CommonMark 规定收尾的 ** 前面是标点、后面紧跟文字时不算收尾，
  「**结论：先说。**后面」会原样露出星号（2026-09-24 真 AI 实测，中文回答里很常见）。
  所以 **…** 先换成两个私用区字符，排好后再把中间那段加粗。

颜色都从 theme 取；换主题后要重新调一次（HTML 里的颜色是写死的）。
"""

from __future__ import annotations

import html
import re

from PySide6.QtGui import (QFont, QFontMetricsF, QTextBlockFormat, QTextCharFormat, QTextCursor,
                           QTextDocument, QTextFormat, QTextListFormat)

from . import theme

PARA_GAP = 8                 # 段距（px）：界面稿 .bot p 的 margin-bottom
LIST_GAP = 4                 # 列表项之间
LINE_SEP = "\u2028"          # 段内换行

_BLOCK_START = re.compile(r"\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```|~~~)")
_FENCE = re.compile(r"\s*(```|~~~)")

_BOLD_OPEN, _BOLD_CLOSE = "\ue000", "\ue001"
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_CODE_SPAN = re.compile(r"(`+)[^`]*?\1")

_MARK = QTextFormat.UserProperty + 1       # 自己写的项目符号（换色时跳过）

_MD_FLAGS = QTextDocument.MarkdownFeature(QTextDocument.MarkdownDialectGitHub.value
                                          | QTextDocument.MarkdownNoHTML.value)


def font(px: float, weight: int = 400) -> QFont:
    """theme.font 的小数字号版（界面稿有 13.5px、11.5px）。"""
    f = theme.font(round(px), weight)
    if px != int(px):
        # QFont 的像素字号只能是整数：换算成磅（按屏幕的逻辑 dpi，一般是 96）
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen()
        dpi = scr.logicalDotsPerInchY() if scr is not None else 96.0
        f.setPointSizeF(px * 72.0 / (dpi or 96.0))
    return f


def qt_line_height(px: float, css: float, weight: int = 400) -> int:
    """CSS 行高（1.78）→ QTextBlockFormat 的比例行高（百分比）。"""
    natural = QFontMetricsF(font(px, weight)).lineSpacing()
    return max(100, round(css * px / natural * 100)) if natural > 0 else round(css * 100)


def prepare_markdown(text: str) -> str:
    """单个换行 → 段内换行；代码块原样；列表、标题、引用、表格的行照常断开。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    fence = False
    join_next = False
    for i, ln in enumerate(lines):
        if _FENCE.match(ln):
            fence = not fence
            out.append(ln)
            join_next = False
            continue
        if fence:
            out.append(ln)
            continue
        ln = _mark_bold(ln)
        if join_next:
            out[-1] += LINE_SEP + ln.lstrip()
        else:
            out.append(ln)
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        join_next = bool(ln.strip() and nxt.strip() and not ln.lstrip().startswith("|")
                         and not _BLOCK_START.match(nxt))
    return "\n".join(out)


def _mark_bold(line: str) -> str:
    """一行里的 **粗体** → 私用区记号包起来（代码片段里的不动）。"""
    out, pos = [], 0
    for m in _CODE_SPAN.finditer(line):
        out.append(_BOLD.sub(lambda b: _BOLD_OPEN + b.group(1) + _BOLD_CLOSE, line[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_BOLD.sub(lambda b: _BOLD_OPEN + b.group(1) + _BOLD_CLOSE, line[pos:]))
    return "".join(out)


def _apply_bold(doc: QTextDocument) -> None:
    """把私用区记号之间的字加粗，再删掉记号。"""
    while True:
        a = doc.find(_BOLD_OPEN)
        if a.isNull():
            break
        start = a.selectionStart()
        a.removeSelectedText()
        b = doc.find(_BOLD_CLOSE, start)
        if b.isNull():
            continue
        end = b.selectionStart()
        b.removeSelectedText()
        if end > start:
            cur = QTextCursor(doc)
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.KeepAnchor)
            f = QTextCharFormat()
            f.setFontWeight(600)
            cur.mergeCharFormat(f)


def split_stable(text: str) -> int:
    """流式输出时，前面哪一段已经写完、以后不会再变（返回切点下标，0 = 还没有）。

    切在最后一个空行处（空行后面已经有字了），而且不能切在代码块中间。
    界面把切点之前的定下来只排一次，之后每来一小段只重排切点之后的那一截。
    """
    pos = 0
    best = 0
    fence = False
    last_content = len(text.rstrip())
    for ln in text.split("\n")[:-1]:          # 最后一行可能还没写完，不看
        end = pos + len(ln) + 1
        if _FENCE.match(ln):
            fence = not fence
        elif not fence and not ln.strip() and end < last_content:
            best = end
        pos = end
    return best


def _doc(px: float, weight: int) -> QTextDocument:
    doc = QTextDocument()
    doc.setDefaultFont(font(px, weight))
    doc.setIndentWidth(18)
    doc.setDocumentMargin(0)
    return doc


def _flatten_lists(doc: QTextDocument, px: float, weight: int) -> set[int]:
    """列表 → 悬挂缩进的段落 + 自己写的符号（「•」/「1.」）。返回列表项的块号。"""
    fm = QFontMetricsF(font(px, weight))
    marks: dict[int, tuple[str, int, bool]] = {}
    block = doc.begin()
    while block.isValid():
        tl = block.textList()
        if tl is not None:
            lf = tl.format()
            numbered = lf.style() in (QTextListFormat.ListDecimal, QTextListFormat.ListLowerAlpha,
                                      QTextListFormat.ListUpperAlpha)
            n = lf.start() + tl.itemNumber(block) if numbered else 0
            marks[block.blockNumber()] = (f"{n}." if numbered else "•", max(1, lf.indent()), numbered)
        block = block.next()
    block = doc.begin()
    while block.isValid():
        tl = block.textList()
        if tl is not None and block.blockNumber() in marks:
            mark, level, numbered = marks[block.blockNumber()]
            tl.remove(block)
            cur = QTextCursor(block)
            fmt = block.blockFormat()
            fmt.setIndent(0)
            indent = round(fm.horizontalAdvance(mark + " "))
            fmt.setLeftMargin(round(px * 1.3) * (level - 1) + max(indent, round(px * 1.15)))
            fmt.setTextIndent(-indent)
            cur.setBlockFormat(fmt)
            cur.movePosition(QTextCursor.StartOfBlock)
            mf = QTextCharFormat()
            mf.setProperty(_MARK, True)
            mf.setForeground(theme.color("t3" if numbered else "t4"))
            cur.insertText(mark + " ", mf)
        block = block.next()
    return set(marks)


def _finish(doc: QTextDocument, px: float, line_height: float, weight: int, tone: str,
            strong: str, items: set[int], para_gap: int) -> None:
    qt_lh = qt_line_height(px, line_height, weight)
    block = doc.begin()
    first = True
    while block.isValid():
        cur = QTextCursor(block)
        fmt = block.blockFormat()
        nxt = block.next()
        heading = fmt.headingLevel() > 0
        fmt.setLineHeight(qt_lh, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        top, bottom = 0, para_gap
        if block.blockNumber() in items and nxt.isValid() and nxt.blockNumber() in items:
            bottom = LIST_GAP
        if heading:
            top, bottom = (0 if first else para_gap // 2), LIST_GAP
            fmt.setHeadingLevel(0)          # 不要 Qt 自己的大标题字号
        if not nxt.isValid():
            bottom = 0
        fmt.setTopMargin(top)
        fmt.setBottomMargin(bottom)
        if fmt.nonBreakableLines():                     # 代码块
            fmt.setBackground(theme.color("bg3"))
            fmt.setLeftMargin(0)
        cur.setBlockFormat(fmt)
        if heading:
            cur.movePosition(QTextCursor.StartOfBlock)
            cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            hf = QTextCharFormat()
            hf.setFont(font(px + 1, 600))
            hf.setForeground(theme.color(strong))
            cur.mergeCharFormat(hf)
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            cf = frag.charFormat()
            fix = QTextCharFormat()
            bold = cf.fontWeight() > 500 or heading
            if cf.fontWeight() > 500 and not heading:
                fix.setFontWeight(600)
            if cf.property(_MARK):
                it += 1
                continue                                   # 项目符号：颜色已经定好
            if cf.isAnchor():
                fix.setForeground(theme.color("acc"))
                fix.setFontUnderline(True)
            elif cf.fontFixedPitch():
                fix.setBackground(theme.color("bg4"))
                fix.setForeground(theme.color(strong))
                fix.setFontFamilies([theme.family()])
            else:
                fix.setForeground(theme.color(strong if bold else tone))
            sel = QTextCursor(doc)
            sel.setPosition(frag.position())
            sel.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
            sel.mergeCharFormat(fix)
            it += 1
        first = False
        block = nxt


def _add_cursor(doc: QTextDocument, key: str) -> None:
    """流式时最后一个字后面的陶土色小方块（界面稿 .cursor：7px 宽）。"""
    cur = QTextCursor(doc)
    cur.movePosition(QTextCursor.End)
    f = QTextCharFormat()
    f.setBackground(theme.color(key))
    f.setForeground(theme.color(key))
    f.setFontWeight(400)
    if cur.block().length() > 1:
        cur.insertText("\u200a", QTextCharFormat())          # 跟字隔开一点
    cur.insertText("\u2002", f)


def _html(doc: QTextDocument) -> str:
    out = doc.toHtml()
    # QLabel 不需要 <!DOCTYPE> 和 <meta>；去掉让字符串短一点
    return re.sub(r"<!DOCTYPE[^>]*>|<meta[^>]*>", "", out)


def md_to_html(text: str, px: float = 14, line_height: float = 1.78, *, tone: str = "t2",
               strong: str = "t1", weight: int = 400, para_gap: int = PARA_GAP,
               cursor: str | None = None) -> str:
    """AI 的回答（Markdown）→ HTML。tone = 正文颜色键、strong = 粗体 / 标题的颜色键。
    cursor = 颜色键时在末尾画流式光标。"""
    doc = _doc(px, weight)
    doc.setMarkdown(prepare_markdown(text), _MD_FLAGS)
    _apply_bold(doc)
    items = _flatten_lists(doc, px, weight)
    _finish(doc, px, line_height, weight, tone, strong, items, para_gap)
    if cursor:
        _add_cursor(doc, cursor)
    return _html(doc)


def plain_html(text: str, px: float = 14, line_height: float = 1.65, *, tone: str = "t1",
               weight: int = 400, para_gap: int = PARA_GAP) -> str:
    """不是 Markdown 的字（主人自己打的问题、引用）：换行照原样，空行当分段。"""
    doc = _doc(px, weight)
    paras = re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())
    doc.setPlainText("\n".join(p.replace("\n", LINE_SEP) for p in paras))
    _finish(doc, px, line_height, weight, tone, tone, set(), para_gap)
    return _html(doc)


def escape(text: str) -> str:
    return html.escape(text, quote=False)


# ── 演示：python -m reader.ui.rich_text ─────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    theme.prepare_platform()
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    first = ""
    books = sorted(Path.home().glob("Downloads/*.epub"))
    if books:
        from reader.books import open_book
        b = open_book(books[0])
        sec = b.sections[min(1, len(b.sections) - 1)]
        first = sec.text()[:160].strip()
    sample = (f"书里这一节开头是：\n「{first}」\n\n" if first else "") + (
        "**先说结论**：公开处决被取消，不是因为人们变仁慈了，而是惩罚的**对象**换了。\n"
        "这一行是单个换行接上的，不应该并到上一行。\n\n"
        "作者给了三条线索：\n"
        "1. 身体不再是惩罚的主要目标\n"
        "2. 惩罚变成了一种「看不见」的程序\n"
        "3. 审判开始关心罪犯的*灵魂*\n\n"
        "- 这是书外的补充：同一时期欧洲各国的刑法改革\n"
        "- 代码字样 `panopticon` 也要能看清\n\n"
        "最后一段：a<b 不能被吃掉。")
    w = QWidget()
    w.setWindowTitle("rich_text 演示")
    theme.style(w, "QWidget {{ background: {bg2}; }}")
    v = QVBoxLayout(w)
    v.setContentsMargins(18, 18, 18, 18)
    lab = QLabel()
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setText(md_to_html(sample, cursor="acc"))
    theme.on_change(lambda _n: lab.setText(md_to_html(sample, cursor="acc")), owner=lab)
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QScrollArea.NoFrame)
    sa.setWidget(lab)
    v.addWidget(sa)
    w.resize(340, 640)
    w.show()
    theme.titlebar(w)
    sys.exit(app.exec())
