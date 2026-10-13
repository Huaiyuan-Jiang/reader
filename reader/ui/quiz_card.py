"""小节小测卡（左栏里，界面稿板 5）。

4 选 1：先点一个选项（选中），再点「就选 X」确认 —— 防手滑。判题用 `Companion.grade`（本地、立刻）。

状态：
    ask     出题，还没选           底部「不想答也可以直接往下读」+ 灰的「就选…」
    pick    选了还没确定           「再点一次别的也行」+「就选 B」
    wrong   第一次错               选错的划掉；AI 说错在哪（不给答案）；右边跳到原文；还能再选一次
            （再选时回到 pick，划掉的那项和提示留着）
    right   答对了                 绿边、「答对了」+ 一句补充；「接着读下一节 ›」
    reveal  第二次还错             直接给正确答案和原因；「接着读下一节 ›」
    skipped 点了「跳过这题」       选项灰掉，写「跳过了，不算对错」（2026-09-25 起：题要能跳过）
            ask / pick / wrong 时都能跳

规矩（UI_PLAN「共同规矩」）：卡出现、点选项、点按钮都**不拿键盘焦点**（NoFocus），←→ 翻页一直归书页。

这个文件里还有左栏共用的小零件：图标、图标按钮、带派生颜色的样式表。
"""

from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..ai.companion import Companion, Grade, Quiz
from ..books import Loc
from . import rich_text, theme

LETTERS = "ABCD"

# ── 图标（取自界面稿的 SVG，16×16，描边 1.5） ─────────────────
_SVG = {
    "fold": '<path d="M8.5 4l-4 4 4 4M12.5 4l-4 4 4 4"/>',
    "unfold": '<path d="M7.5 4l4 4-4 4M3.5 4l4 4-4 4"/>',
    "send": '<path d="M8 13V3.5M4 7.5l4-4 4 4"/>',
    "stop": '<rect x="4.5" y="4.5" width="7" height="7" rx="1.5"/>',
    "x": '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/>',
    "ok": '<path d="M3 8.5l3 3 7-7"/>',
    "retry": '<path d="M13 8a5 5 0 11-1.5-3.6M13 2.5v3h-3"/>',
    "quote": '<path d="M3 4.5h10M3 8h10M3 11.5h6.5"/>',
}


def icon_pixmap(name: str, key: str, px: int = 16, *, stroke: float = 1.5,
                dpr: float = 2.0) -> QPixmap:
    """画一个界面稿里的线条图标。key = theme 颜色键（或 #hex）。"""
    col = key if key.startswith("#") else theme.color(key).name()
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" stroke="{col}" '
           f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{_SVG[name]}</svg>')
    pm = QPixmap(round(px * dpr), round(px * dpr))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF(0, 0, pm.width(), pm.height()))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def derived() -> dict[str, str]:
    """界面稿里用 color-mix 算出来的颜色（跟着当前主题）。"""
    m = theme.mix
    return {
        "quiz_b": m("acc", "bg3", 0.60), "quiz_ok_b": m("green", "bg3", 0.55),
        "opt_ok_b": m("green", "bg3", 0.65), "opt_ok_bg": m("green", "bg3", 0.13),
        "opt_no_b": m("red", "bg3", 0.45), "key_no_b": m("red", "bg3", 0.60),
        "off_bg": m("bg4", "bg3", 0.42), "off_b": m("l2", "bg3", 0.42), "off_t": m("t1", "bg3", 0.42),
        "me_quote": m("bg2", "bg4", 0.55),
        "err_b": m("red", "bg3", 0.40),
    }


def sheet(template: str) -> str:
    return template.format(**theme.THEMES[theme.current()], **derived())


def restyle(widget: QWidget, template: str) -> None:
    """套样式（可以用 derived() 里的键），换主题时自动重套。"""
    widget.setStyleSheet(sheet(template))
    import weakref
    ref = weakref.ref(widget)

    def again(_name: str) -> None:
        w = ref()
        if w is not None:
            try:
                w.setStyleSheet(sheet(template))
            except RuntimeError:
                pass
    theme.on_change(again, owner=widget)


def repolish(w: QWidget) -> None:
    """动态属性变了以后让样式表重新生效。"""
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


# 左栏共用的按钮样式（小按钮、主按钮、「就选…」没选时的样子）
BUTTONS = """
QPushButton[sz="sm"] {{ min-height:28px; max-height:28px; height:28px; padding:0 10px; font-size:12px;
  border-radius:6px; background:{bg4}; border:1px solid {l2}; color:{t1}; }}
QPushButton[sz="sm"]:hover {{ background:{bg5}; }}
QPushButton[sz="sm"][kind="pri"] {{ background:{pri}; border-color:{pri}; color:{pri_ink}; font-weight:600; }}
QPushButton[sz="sm"][kind="pri"]:hover {{ background:{pri}; }}
QPushButton[sz="sm"]:disabled {{ background:{off_bg}; border-color:{off_b}; color:{off_t}; }}
"""


class SmallButton(QPushButton):
    """界面稿 .btn.sm：28px 高。不拿焦点（点了也不把焦点从书页拿走）。"""

    def __init__(self, text: str, kind: str | None = None, icon: str | None = None, parent=None):
        super().__init__(text, parent)
        self.setProperty("sz", "sm")
        if kind:
            self.setProperty("kind", kind)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._icon = icon
        if icon:
            self.setIconSize(QSize(14, 14))
            self._on_theme()
            theme.on_change(self._on_theme)

    def _on_theme(self, _name: str = "") -> None:
        if self._icon:
            self.setIcon(QIcon(icon_pixmap(self._icon, "t1", 14)))


def small_button(text: str, kind: str | None = None, icon: str | None = None) -> QPushButton:
    return SmallButton(text, kind, icon)


class IconButton(QAbstractButton):
    """只有图标的小按钮（收起、去掉引用）：悬停出底色，一定带工具提示。不拿焦点。"""

    def __init__(self, name: str, tip: str, size: int = 28, icon_px: int = 16, key: str = "t4",
                 parent=None):
        super().__init__(parent)
        self._name, self._key, self._icon_px = name, key, icon_px
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def set_icon(self, name: str) -> None:
        self._name = name
        self.update()

    def sizeHint(self) -> QSize:
        return self.size()

    def enterEvent(self, e) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() and self.isEnabled():
            p.setPen(Qt.NoPen)
            p.setBrush(theme.color("bg4"))
            p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        key = "t2" if self.underMouse() else self._key
        pm = icon_pixmap(self._name, key, self._icon_px, dpr=self.devicePixelRatioF() * 2)
        x = (self.width() - self._icon_px) / 2
        y = (self.height() - self._icon_px) / 2
        p.drawPixmap(QRectF(x, y, self._icon_px, self._icon_px), pm, QRectF(pm.rect()))
        p.end()


def rich_label(html: str = "", *, selectable: bool = True) -> QLabel:
    lab = QLabel()
    lab.setTextFormat(Qt.RichText)
    lab.setWordWrap(True)
    lab.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lab.setFocusPolicy(Qt.NoFocus)               # 选字可以，但点一下不抢书页的 ←→
    lab.setText(html)
    return lab


# ═══════════════════════════════════════════════════════════
# 选项
# ═══════════════════════════════════════════════════════════

class _Key(QWidget):
    """选项前面的 20×20 小方块：字母 / 选中（实心）/ 对（绿底勾）/ 错（红边叉）。"""

    def __init__(self, letter: str, parent=None):
        super().__init__(parent)
        self.letter = letter
        self.state = ""
        self.setFixedSize(20, 20)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0.5, 0.5, 19, 19)
        d = derived()
        if self.state == "on":
            p.setPen(theme.color("t1"))
            p.setBrush(theme.color("t1"))
        elif self.state == "ok":
            p.setPen(theme.color("green"))
            p.setBrush(theme.color("green"))
        elif self.state == "no":
            p.setPen(QColor(d["key_no_b"]))
            p.setBrush(Qt.NoBrush)
        else:
            p.setPen(theme.color("l3"))
            p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 5, 5)
        if self.state in ("ok", "no"):
            name, key, s = ("ok", "acc_ink", 13) if self.state == "ok" else ("x", "redt", 12)
            pm = icon_pixmap(name, key, s, stroke=1.8, dpr=self.devicePixelRatioF() * 2)
            p.drawPixmap(QRectF((20 - s) / 2, (20 - s) / 2, s, s), pm, QRectF(pm.rect()))
        else:
            p.setPen(theme.color("bg2" if self.state == "on" else "t3"))
            p.setFont(rich_text.font(11, 500))
            p.drawText(self.rect(), Qt.AlignCenter, self.letter)
        p.end()


class Option(QFrame):
    """一个选项。点一下（松开时）= 选中它；不拿焦点。"""

    clicked = Signal(int)

    def __init__(self, idx: int, text: str, parent=None):
        super().__init__(parent)
        self.idx = idx
        self.text = text
        self.state = ""
        self.enabled = True
        self._pressed = False
        self.setObjectName("opt")
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_Hover)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(10)
        self.key = _Key(LETTERS[idx])
        h.addWidget(self.key, 0, Qt.AlignTop)
        self.label = rich_label(selectable=False)
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents)
        h.addWidget(self.label, 1)
        self.key.setContentsMargins(0, 1, 0, 0)
        self._render()

    def set_state(self, state: str, enabled: bool) -> None:
        self.state, self.enabled = state, enabled
        self.key.state = state
        self.setProperty("st", state)
        self.setProperty("live", enabled)
        self.setCursor(Qt.PointingHandCursor if enabled else Qt.ArrowCursor)
        repolish(self)
        self._render()
        self.key.update()

    def _render(self) -> None:
        tone = {"on": "t1", "ok": "t1", "no": "t4"}.get(self.state, "t2")
        html = rich_text.plain_html(self.text, 13.5, 1.55, tone=tone)
        if self.state == "no":
            html = html.replace("<span style=\"", "<span style=\"text-decoration: line-through; ")
        self.label.setText(html)

    def rerender(self) -> None:
        self._render()
        self.key.update()

    def mousePressEvent(self, e) -> None:
        self._pressed = e.button() == Qt.LeftButton
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        # 动作在松开时执行；按下后移出去再松开 = 取消（AF 18 章）
        inside = self.rect().contains(e.position().toPoint())
        if self._pressed and inside and self.enabled and e.button() == Qt.LeftButton:
            self.clicked.emit(self.idx)
        self._pressed = False
        e.accept()


# ═══════════════════════════════════════════════════════════
# 卡
# ═══════════════════════════════════════════════════════════

CARD = """
QFrame#quiz {{ background:{bg3}; border:1px solid {quiz_b}; border-radius:12px; }}
QFrame#quiz[ok="true"] {{ border-color:{quiz_ok_b}; }}
QLabel#qh {{ color:{acc}; background:transparent; }}
QFrame#quiz[ok="true"] QLabel#qh {{ color:{greent}; }}
QLabel#qcount, QLabel#qcap {{ color:{t4}; background:transparent; }}
QLabel#qnote {{ color:{t3}; background:transparent; }}
QLabel#verdict[good="true"] {{ color:{greent}; }}
QLabel#verdict[good="false"] {{ color:{redt}; }}
QLabel {{ background:transparent; }}
QFrame#opt {{ background:transparent; border:1px solid {l2}; border-radius:9px; }}
QFrame#opt[live="true"]:hover {{ background:{bg4}; }}
QFrame#opt[st="on"] {{ border-color:{focus}; background:{bg4}; }}
QFrame#opt[st="ok"] {{ border-color:{opt_ok_b}; background:{opt_ok_bg}; }}
QFrame#opt[st="no"] {{ border-color:{opt_no_b}; background:transparent; }}
""" + BUTTONS


class QuizCard(QFrame):
    """一道题。信号：
    wrong(Loc, quote)       答错（第一次或第二次）→ 右边跳到原文（Loc 为 None 时不发）
    finished(section, ok)   答完（对了，或者两次都错）
    next_section()          点了「接着读下一节 ›」（读书界面帮主人翻到下一节）
    skipped(section)        点了「跳过这题」（不算对错，这一节不再出题）
    activity()              点选项、确认都算读书活动
    """

    wrong = Signal(object, str)
    finished = Signal(int, bool)
    next_section = Signal()
    skipped = Signal(int)
    activity = Signal()

    def __init__(self, quiz: Quiz, section_title: str = "",
                 page_label: Callable[[Loc], str] | None = None, parent=None):
        super().__init__(parent)
        self.quiz = quiz
        self.section_title = section_title
        self.page_label = page_label
        self.state = "ask"
        self.picked: int | None = None
        self.crossed: set[int] = set()
        self.grade: Grade | None = None
        self.timed_out = False
        self.continued = False
        self.src_page = ""               # 跳过去以后书页报回来的页码（EPUB 的页码按排版算，只有书页知道）
        self.setObjectName("quiz")
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        restyle(self, CARD)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 13, 14, 12)
        v.setSpacing(0)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel(objectName="qh")
        self.title.setFont(rich_text.font(12, 600))
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        head.addWidget(self.title, 1)
        count = QLabel("1 道题", objectName="qcount")
        count.setFont(rich_text.font(12))
        head.addWidget(count, 0)
        v.addLayout(head)

        self.question = rich_label()
        v.addSpacing(7)
        v.addWidget(self.question)
        v.addSpacing(11)

        self.options: list[Option] = []
        for i, text in enumerate(quiz.options):
            o = Option(i, text)
            o.clicked.connect(self._pick)
            self.options.append(o)
            v.addWidget(o)
            v.addSpacing(6)

        self.verdict = QLabel(objectName="verdict")
        self.verdict.setFont(rich_text.font(13.5, 600))
        self.verdict_row = QWidget()
        vr = QHBoxLayout(self.verdict_row)
        vr.setContentsMargins(0, 4, 0, 0)
        vr.setSpacing(6)
        self.verdict_icon = QLabel()
        self.verdict_icon.setFixedSize(16, 16)
        vr.addWidget(self.verdict_icon, 0, Qt.AlignVCenter)
        vr.addWidget(self.verdict, 1)
        v.addWidget(self.verdict_row)
        self.explain = rich_label()
        self.explain.setContentsMargins(0, 10, 0, 0)
        v.addWidget(self.explain)

        self.note = QLabel("时间到了，这题答不答都行", objectName="qnote")
        self.note.setFont(rich_text.font(12))
        self.note.setContentsMargins(0, 10, 0, 0)
        v.addWidget(self.note)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 10, 0, 0)
        foot.setSpacing(8)
        self.cap = QLabel(objectName="qcap")
        self.cap.setFont(rich_text.font(12))
        self.cap.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        foot.addWidget(self.cap, 1)
        self.cap2 = QLabel(objectName="qcap")
        self.cap2.setFont(rich_text.font(12))
        foot.addWidget(self.cap2, 0)
        self.skip = small_button("跳过这题", "ghost")
        self.skip.setToolTip("不答了：不算对错，这一节不再出题")
        self.skip.clicked.connect(self._skip)
        foot.addWidget(self.skip, 0)
        self.button = small_button("就选…", "pri")
        self.button.clicked.connect(self._button)
        foot.addWidget(self.button, 0)
        v.addLayout(foot)

        theme.on_change(self._on_theme)
        self._render()

    def _on_theme(self, _name: str = "") -> None:
        self._render()

    # ── 外部 ─────────────────────────────────────────────
    @property
    def done(self) -> bool:
        """答完了或者跳过了（不再等主人答）。"""
        return self.state in ("right", "reveal", "skipped")

    def timeout_note(self) -> None:
        """锁屏时间到了：卡上写一句，答不答都行（还能答）。"""
        self.timed_out = True
        self._render()

    def set_source_page(self, page: str) -> None:
        """「原文在第 134 页」：书页跳到出处后告诉卡页码。"""
        self.src_page = page
        self._render()

    # ── 交互 ─────────────────────────────────────────────
    def _pick(self, i: int) -> None:
        if self.done or i in self.crossed:
            return
        self.picked = i
        self.state = "pick"
        self.activity.emit()
        self._render()

    def _skip(self) -> None:
        if self.done:
            return
        self.state = "skipped"
        self.picked = None
        self.activity.emit()
        self._render()
        self.skipped.emit(self.quiz.section)

    def _button(self) -> None:
        if self.state == "skipped":
            return
        if self.state == "pick" and self.picked is not None:
            self._confirm(self.picked)
        elif self.done:
            self.continued = True
            self._render()
            self.activity.emit()
            self.next_section.emit()

    def _confirm(self, i: int) -> None:
        g = Companion.grade(self.quiz, i)
        self.grade = g
        self.activity.emit()
        if g.correct:
            self.state = "right"
        else:
            self.crossed.add(i)
            self.state = "reveal" if self.quiz.finished else "wrong"
        self.picked = None
        self._render()
        if not g.correct and g.quote_loc is not None:
            self.wrong.emit(g.quote_loc, g.quote)
        if self.quiz.finished:
            self.finished.emit(self.quiz.section, g.correct)

    # ── 画 ───────────────────────────────────────────────
    def _page(self) -> str:
        loc = self.grade.quote_loc if self.grade else self.quiz.quote_loc
        if loc is None:
            return ""
        if self.page_label is None:
            return self.src_page
        try:
            return self.page_label(loc) or ""
        except Exception:  # noqa: BLE001 —— 页码只是个提示
            return ""

    def _render(self) -> None:
        st = self.state
        title = f"小测 · {self.section_title}" if self.section_title else "小测"
        fm = self.title.fontMetrics()
        self.title.setText(fm.elidedText(title, Qt.ElideRight, 230))
        self.title.setToolTip(title if len(title) > 20 else "")
        self.setProperty("ok", st == "right")
        repolish(self)
        repolish(self.title)
        self.question.setText(rich_text.plain_html(self.quiz.question, 15, 1.65, tone="t1", weight=500))

        answer = self.quiz.answer
        for o in self.options:
            if st == "right" and o.idx == answer:
                o.set_state("ok", False)
            elif st == "reveal" and o.idx == answer:
                o.set_state("ok", False)
            elif o.idx in self.crossed:
                o.set_state("no", False)
            elif st == "pick" and o.idx == self.picked:
                o.set_state("on", True)
            else:
                o.set_state("", not self.done)

        g = self.grade
        show_verdict = st in ("right", "wrong", "reveal") or (st == "pick" and g is not None)
        self.verdict_row.setVisible(show_verdict)
        self.explain.setVisible(show_verdict)
        if show_verdict and g is not None:
            good = g.correct
            self.verdict.setText({"right": "答对了", "reveal": "还是不对"}.get(st, "不太对"))
            self.verdict.setProperty("good", good)
            repolish(self.verdict)
            self.verdict_icon.setPixmap(icon_pixmap("ok" if good else "x", "greent" if good else "redt", 16))
            if st == "reveal":
                text = f"正确答案是 **{self.quiz.answer_text}**。{self.quiz.explain}".strip()
            elif good and re.fullmatch(r"(对|对的|答对了|正确)[。！!]?", (self.quiz.hints[answer] or "").strip()):
                text = self.quiz.explain or g.explanation     # 提示只有一个「对。」：只留解释
            elif good:
                text = g.explanation
            else:
                hint = g.explanation.rstrip()
                more = ("**右边已经跳到那一段**，看一眼再选一次。" if g.quote_loc is not None
                        else "回原文里找找，再选一次。")
                text = f"{hint}{'' if hint.endswith(('。', '！', '？')) or not hint else '。'}{more}"
            text = re.sub(r"([。！？；：，」])[ \t]+", r"\1", text)      # 中文标点后不要空格
            self.explain.setText(rich_text.md_to_html(text, 13.5, 1.75, tone="t2", strong="t1"))

        self.note.setVisible(self.timed_out and not self.done)

        self.cap2.hide()
        self.button.show()
        self.button.setEnabled(True)
        self.skip.setVisible(not self.done)
        if st == "skipped":
            self.cap.setText("跳过了，不算对错")
            self.button.hide()
        elif st == "ask":
            self.cap.setText("答了也会记进读书笔记" if self.timed_out else "不想答也可以直接往下读")
            self.button.setText("就选…")
            self.button.setEnabled(False)
        elif st == "pick":
            self.cap.setText("再点一次别的也行")
            self.button.setText(f"就选 {LETTERS[self.picked]}")
        elif st == "wrong":
            self.cap.setText("还有 1 次机会")
            self.button.hide()
            page = self._page()
            if page:
                self.cap2.setText(f"原文在{page}" if page.startswith("第") else f"原文在 {page}")
                self.cap2.show()
        elif self.continued:                     # right / reveal，点过「接着读」
            self.cap.setText("记进了这次的读书笔记")
            self.button.hide()
        else:                                    # right / reveal
            self.cap.setText("记进这次的读书笔记")
            self.button.setText("接着读下一节 ›")
        for o in self.options:
            o.rerender()


# ── 演示：python -m reader.ui.quiz_card ────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path

    theme.prepare_platform()
    from PySide6.QtWidgets import QApplication, QPushButton as _Btn

    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    from reader.books import open_book
    from reader.ai.companion import locate_quote

    books = sorted(Path.home().glob("Downloads/*.epub"))
    book = open_book(books[0]) if books else None
    sec = book.sections[min(1, len(book.sections) - 1)] if book else None
    quote = ""
    if sec is not None:
        body = [ln.strip() for ln in sec.text().split("\n") if len(ln.strip()) > 40]
        quote = body[len(body) // 2][:60] if body else ""
    quiz = Quiz(sec.idx if sec else 0, "按这一节的说法，公开处决为什么渐渐消失了？",
                ["因为处决花钱太多", "因为人们觉得它和犯罪一样野蛮", "因为没人再去围观", "因为法律明文禁止"],
                1, ["书里没提钱，回原文看看作者怎么说观众的感受。", "对。", "恰恰相反，观众很多。", "不是法律的原因。"],
                "原文说，这种惩罚方式的野蛮程度不亚于犯罪本身。", quote)
    if book is not None and sec is not None:
        quiz.quote_loc = locate_quote(book, sec, quote)

    w = QWidget()
    w.setWindowTitle("小测卡演示")
    theme.style(w, "QWidget#root {{ background: {bg2}; }}")
    w.setObjectName("root")
    v = QVBoxLayout(w)
    v.setContentsMargins(18, 18, 18, 18)
    card = QuizCard(quiz, sec.title if sec else "", lambda loc: f"第 {loc.doc + 1} 个文件")
    card.wrong.connect(lambda loc, q: print("跳到原文", loc, q[:20]))
    card.finished.connect(lambda s, ok: print("答完", s, ok))
    card.next_section.connect(lambda: print("接着读下一节"))
    v.addWidget(card)
    v.addStretch(1)
    t = _Btn("时间到了")
    t.clicked.connect(card.timeout_note)
    s = _Btn("换主题")
    s.clicked.connect(lambda: theme.set_theme("light" if theme.current() == "dark" else "dark", save=False))
    v.addWidget(t)
    v.addWidget(s)
    w.resize(340, 800)
    w.show()
    sys.exit(app.exec())
