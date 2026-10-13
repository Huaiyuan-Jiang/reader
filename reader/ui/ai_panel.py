"""左栏「问 AI」（界面稿板 3、4、5 的左栏，板 11 的没登录）。

接口照 docs/UI_PLAN.md「C」：
    activity / jump_to(Loc, quote) / quiz_finished(小节号, 最后是否答对) / collapsed_changed(bool)
    set_companion / set_context / attach_quote / show_quiz / quiz_timeout_note / stop
另外多一个信号 next_section()：小测卡上点了「接着读下一节 ›」（界面稿：「就是帮你翻页」）。

## 怎么排
- 顶上 54px：「问 AI」（有题没答完时后面一个陶土色小点）+「看得到：第三章 · 3.2 节 · 第 134 页」+ 收起。
- 中间一条往下排的消息：主人的话是右边的灰底气泡；AI 的话没有气泡，正文 14px、行高 1.78、t2 色。
  消息少时贴着底（靠近输入框），跟聊天软件一样。
- 底下输入框：引用块（可以 × 掉）+ 输入 + 左边「Ctrl+Enter」键帽 / 回答时换成「停下」+ 右边发送。

## 流式
`Companion.ask` 在工作线程里跑，每个 Delta 用信号送回界面线程。界面每 40ms 最多排一次版，
而且只排**正在写的这一段回答**里还会变的那一截：写完的段落（空行之前）定下来只排一次，
之前的消息一个都不碰。

## 焦点（UI_PLAN 共同规矩）
只有主人自己点「问 AI」（attach_quote）或点输入框时焦点才进输入框。出题、出错、回答到了都不拿焦点；
按钮、选项、消息文字都是 NoFocus，点了也不会把 ←→ 从书页那边拿走。

## 出错
不弹窗、不用会自己消失的提示条：在回答的位置留一行字 +「重试」，重试就在原地重新回答。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontMetricsF, QPainter, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (QAbstractButton, QFrame, QGraphicsOpacityEffect, QHBoxLayout,
                               QLabel, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget)

from ..ai.claude import LOGIN_HINT, Delta, Result
from ..ai.companion import Companion, Quiz
from ..books import Loc
from . import rich_text, theme
from .quiz_card import (BUTTONS, IconButton, QuizCard, icon_pixmap, repolish, restyle, rich_label,
                        small_button)

WIDTH = 340
RAIL = 44                       # 收起后留的一条（放「展开」按钮和小点）
RENDER_MS = 40                  # 流式排版最多每 40ms 一次
ASK_EXPLAIN = "解释一下这段"
PH_DEFAULT = "问这本书的任何事…"
PH_QUOTE = "直接发送就是「解释一下这段」，也可以接着写"
PH_QUIZ = "题可以先不答，想问什么直接问"
PH_NOLOGIN = "AI 没登录，这次问不了"
QUOTE_SHOW = 140                # 引用块最多显示这么多字（发给 AI 的是全文）

_LIVE_THREADS: set[QThread] = set()     # 线程跑完前不能被回收（面板先没了也一样）


# ═══════════════════════════════════════════════════════════
# 工作线程
# ═══════════════════════════════════════════════════════════

class AskThread(QThread):
    """在后台跑一次 Companion.ask。信号自动排队回到界面线程。"""

    delta = Signal(int, str)
    done = Signal(int, object)          # Result

    def __init__(self, gen: int, companion: Companion, question: str, loc: Loc, selection: str):
        super().__init__()
        self.gen = gen
        self.companion, self.question, self.loc, self.selection = companion, question, loc, selection

    def run(self) -> None:
        final = Result(error="没有结果")
        try:
            for ev in self.companion.ask(self.question, self.loc, self.selection):
                if isinstance(ev, Delta):
                    if ev.text:
                        self.delta.emit(self.gen, ev.text)
                elif isinstance(ev, Result):
                    final = ev
        except Exception as e:  # noqa: BLE001 —— 线程里的异常不能丢，要变成那一行错误
            final = Result(error=f"{type(e).__name__}: {e}")
        self.done.emit(self.gen, final)


def friendly_error(err: str) -> str:
    """Result.error → 给主人看的一句话。"""
    if err == LOGIN_HINT or "没登录" in err:
        return "AI 没登录上（reader 专用的 Claude 账号）。书照常能读，这次读完回书架登录一次就好。"
    low = err.lower()
    if "超时" in err or "timeout" in low:
        return "AI 太久没回音，超时了。"
    if any(w in low for w in ("usage limit", "rate limit", "quota", "limit reached", "overloaded")) \
            or "额度" in err:
        return "这个账号的额度暂时用完了，过一会儿再试。"
    short = err.strip().splitlines()[0] if err.strip() else "不知道哪里出了问题"
    return f"没答上来：{short[:120]}"


# ═══════════════════════════════════════════════════════════
# 消息
# ═══════════════════════════════════════════════════════════

@dataclass
class Quote:
    text: str
    loc: Loc
    label: str                     # 「引用 · 第 134 页」


def _quote_text(text: str) -> str:
    t = re.sub(r"\s+", " ", text.strip())
    return t if len(t) <= QUOTE_SHOW else t[:QUOTE_SHOW - 1] + "…"


class QuoteBox(QFrame):
    """引用块：输入框里（bg3，带 ×）和主人的气泡里（淡一点，不带 ×）。"""

    removed = Signal()

    def __init__(self, quote: Quote, *, removable: bool, parent=None):
        super().__init__(parent)
        self.quote = quote
        self.setObjectName("quoteIn" if removable else "quoteMe")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 7, 10 if not removable else 6, 8)
        v.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(4)
        cap = QLabel(quote.label, objectName="qhd")
        cap.setFont(rich_text.font(11))
        head.addWidget(cap, 1)
        if removable:
            x = IconButton("x", "去掉引用", size=20, icon_px=14)
            x.clicked.connect(self.removed.emit)
            head.addWidget(x, 0, Qt.AlignVCenter)
        v.addLayout(head)
        self.body = rich_label(selectable=not removable)
        v.addWidget(self.body)
        self.rerender()

    def rerender(self) -> None:
        self.body.setText(rich_text.plain_html(f"「{_quote_text(self.quote.text)}」", 13, 1.65, tone="t2"))

    def natural_width(self) -> float:
        fm = QFontMetricsF(rich_text.font(13))
        return fm.horizontalAdvance(f"「{_quote_text(self.quote.text)}」") + 22


class Bubble(QFrame):
    """主人说的话：右边灰底气泡（界面稿 .me），宽度按字多少走，最宽 90%。"""

    def __init__(self, text: str, quote: Quote | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("me")
        self.text = text
        self.quote_box = QuoteBox(quote, removable=False) if quote else None
        v = QVBoxLayout(self)
        v.setContentsMargins(13, 9, 13, 9)
        v.setSpacing(7)
        if self.quote_box:
            v.addWidget(self.quote_box)
        self.label = rich_label()
        v.addWidget(self.label)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.rerender()

    def rerender(self) -> None:
        self.label.setText(rich_text.plain_html(self.text, 14, 1.65, tone="t1"))
        if self.quote_box:
            self.quote_box.rerender()

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(rich_text.font(14))
        natural = max((fm.horizontalAdvance(ln) for ln in self.text.split("\n")), default=0) + 4
        if self.quote_box:
            natural = max(natural, self.quote_box.natural_width())
        w = int(min(self.maximumWidth(), natural + 26 + 2))
        return QSize(w, self.heightForWidth(w))


class BubbleRow(QWidget):
    def __init__(self, bubble: Bubble, parent=None):
        super().__init__(parent)
        self.bubble = bubble
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addStretch(1)
        h.addWidget(bubble)

    def resizeEvent(self, e) -> None:
        w = int(self.width() * 0.9)
        if self.bubble.maximumWidth() != w:
            self.bubble.setMaximumWidth(w)
            self.bubble.updateGeometry()
        super().resizeEvent(e)

    def rerender(self) -> None:
        self.bubble.rerender()


class Reply(QWidget):
    """AI 的一段回答（界面稿 .bot）。流式：写完的段落各自一个标签、只排一次；
    还在写的那一截单独一个标签，每次只重排它。"""

    retry = Signal()

    def __init__(self, question: str, loc: Loc, selection: str, parent=None):
        super().__init__(parent)
        self.question, self.loc, self.selection = question, loc, selection
        self.buffer = ""
        self.frozen = 0                 # buffer[:frozen] 已经定下来了
        self.chunks: list[tuple[str, QLabel]] = []
        self.live = False
        self.renders = 0                # 测试用：排了几次版
        self.error: str | None = None
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(rich_text.PARA_GAP)
        self.tail = rich_label()
        self.v.addWidget(self.tail)
        self.status = QWidget()
        self.status.hide()
        self.v.addWidget(self.status)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(RENDER_MS)
        self._timer.timeout.connect(self._render)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    # ── 流式 ─────────────────────────────────────────────
    def begin(self) -> None:
        """开始（或重试）：清掉上次的字和错误，显示「在想…」和光标。"""
        for _t, lab in self.chunks:
            lab.hide()
            lab.deleteLater()
        self.chunks.clear()
        self.buffer, self.frozen, self.error = "", 0, None
        self._clear_status()
        self.live = True
        self._render()

    def feed(self, text: str) -> None:
        self.buffer += text
        if not self._timer.isActive():
            self._timer.start()

    def finish(self, result: Result) -> None:
        self._timer.stop()
        if not self.buffer.strip() and result.ok and result.text:
            self.buffer = result.text       # 没收到流式片段：整段补上
        self.live = False
        self._render()
        self.error = result.error
        if result.error == "已停止":
            self._set_status(stopped=True)
        elif result.error:
            self._set_status(message=friendly_error(result.error))

    def flush(self) -> None:
        """立刻把攒着的字排出来（测试、收尾用）。"""
        self._timer.stop()
        self._render()

    def _render(self) -> None:
        cut = rich_text.split_stable(self.buffer)
        # 写完的段落定下来。收尾时也照样切：字要是一口气全到了（没赶上 40ms 的排版），
        # 结果也跟一段段到的一样，不会整段挤在 tail 里（测试曾因此时有时无地失败）
        if cut > self.frozen:
            piece = self.buffer[self.frozen:cut].strip("\n")
            if piece.strip():
                lab = rich_label(self._md(piece))
                self.v.insertWidget(len(self.chunks), lab)
                self.chunks.append((piece, lab))
            self.frozen = cut
        rest = self.buffer[self.frozen:].strip("\n")
        if self.live:
            html = self._md(rest, cursor=True) if rest.strip() else \
                rich_text.md_to_html("在想…", 14, 1.78, tone="t4", cursor="acc")
            self.tail.setText(html)
            self.tail.show()
        elif rest.strip():
            self.tail.setText(self._md(rest))
            self.tail.show()
        else:
            self.tail.clear()
            self.tail.hide()
        self.renders += 1

    def _md(self, text: str, cursor: bool = False) -> str:
        return rich_text.md_to_html(text, 14, 1.78, tone="t2", strong="t1",
                                    cursor="acc" if cursor else None)

    def rerender(self) -> None:
        for piece, lab in self.chunks:
            lab.setText(self._md(piece))
        self._render()
        if not self.status.isHidden():
            self._set_status(stopped=self.error == "已停止",
                             message=None if self.error == "已停止" else friendly_error(self.error or ""))

    # ── 停下 / 出错 ──────────────────────────────────────
    def _clear_status(self) -> None:
        self.v.removeWidget(self.status)
        self.status.hide()
        self.status.deleteLater()
        self.status = QWidget()
        self.status.hide()
        self.v.addWidget(self.status)

    def _set_status(self, *, stopped: bool = False, message: str | None = None) -> None:
        self._clear_status()
        if stopped:
            lab = QLabel("停下了", objectName="cap")
            lab.setFont(rich_text.font(12))
            lay = QHBoxLayout(self.status)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(lab)
            lay.addStretch(1)
        else:
            self.status.setObjectName("err")
            self.status.setAttribute(Qt.WA_StyledBackground, True)
            lay = QHBoxLayout(self.status)
            lay.setContentsMargins(12, 9, 8, 9)
            lay.setSpacing(10)
            dot = _Dot("red", 7)
            lay.addWidget(dot, 0, Qt.AlignTop)
            dot.setContentsMargins(0, 0, 0, 0)
            text = rich_label(rich_text.plain_html(message or "", 13, 1.6, tone="t2"))
            self.err_label = text
            lay.addWidget(text, 1)
            self.retry_button = small_button("重试", icon="retry")
            self.retry_button.setToolTip("在这里重新回答一次")
            self.retry_button.clicked.connect(self.retry.emit)
            lay.addWidget(self.retry_button, 0, Qt.AlignVCenter)
        self.status.show()

    def set_retry_enabled(self, on: bool) -> None:
        b = getattr(self, "retry_button", None)
        if b is not None:
            try:
                b.setEnabled(on)
            except RuntimeError:
                pass


class _Dot(QWidget):
    """小圆点（有题没答完的陶土色点、出错的红点）。"""

    def __init__(self, key: str = "acc", size: int = 7, parent=None):
        super().__init__(parent)
        self.key = key
        self.setFixedSize(size, size + 6)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.color(self.key))
        s = self.width()
        p.drawEllipse(QRectF(0, (self.height() - s) / 2, s, s))
        p.end()


# ═══════════════════════════════════════════════════════════
# 输入框
# ═══════════════════════════════════════════════════════════

class _Input(QTextEdit):
    """Ctrl+Enter 发送，Enter 换行。只收纯文字；行高 1.6（界面稿 .composer .typed）。"""

    send = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFont(rich_text.font(14))
        self.document().setDocumentMargin(0)
        self.setTabChangesFocus(True)
        # 只有点它（或 attach_quote 里明确给焦点）才进来：窗口刚打开时 Qt 会把焦点给第一个能 Tab 到的
        # 控件，不能是这里，不然 ←→ 翻不了页
        self.setFocusPolicy(Qt.ClickFocus)
        self._lh = rich_text.qt_line_height(14, 1.6)
        self.reset_format()

    def reset_format(self) -> None:
        cur = QTextCursor(self.document())
        cur.select(QTextCursor.Document)
        fmt = QTextBlockFormat()
        fmt.setLineHeight(self._lh, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cur.mergeBlockFormat(fmt)

    def clear_text(self) -> None:
        self.clear()
        self.reset_format()

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and e.modifiers() & Qt.ControlModifier:
            e.accept()
            self.send.emit()
            return
        super().keyPressEvent(e)

    def insertFromMimeData(self, source) -> None:
        # 粘贴也只要纯文字，并且保持行高
        if source.hasText():
            self.textCursor().insertText(source.text())
            self.reset_format()
            return
        super().insertFromMimeData(source)


class _SendButton(QAbstractButton):
    """30×30 发送按钮（界面稿 .send）：能发 = 主色；不能发 = 灰。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setToolTip("发送（Ctrl+Enter）")
        self.setAccessibleName("发送")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(30, 30)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on = self.isEnabled()
        p.setPen(Qt.NoPen)
        p.setBrush(theme.color("pri" if on else "bg4"))
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        pm = icon_pixmap("send", "pri_ink" if on else "t5", 16, dpr=self.devicePixelRatioF() * 2)
        p.drawPixmap(QRectF(7, 7, 16, 16), pm, QRectF(pm.rect()))
        p.end()


class Composer(QFrame):
    """输入区（界面稿 .composer）。"""

    send = Signal()
    stop = Signal()
    typed = Signal()
    quote_removed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composer")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.quote_box: QuoteBox | None = None
        self._placeholder = PH_DEFAULT
        self._draft = ""
        self._locked = False
        self._streaming = False
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 8)
        v.setSpacing(0)
        self.quote_slot = QVBoxLayout()
        self.quote_slot.setContentsMargins(0, 0, 0, 0)
        v.addLayout(self.quote_slot)
        self.input = _Input()
        self.input.setObjectName("input")
        self.input.send.connect(self.send.emit)
        self.input.textChanged.connect(self._changed)
        self.input.installEventFilter(self)
        v.addWidget(self.input)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 4, 0, 0)
        bar.setSpacing(8)
        self.kbd = QLabel("Ctrl+Enter", objectName="kbd")
        self.kbd.setFont(rich_text.font(11))
        self.kbd.setToolTip("Ctrl+Enter 发送，Enter 换行")
        bar.addWidget(self.kbd, 0, Qt.AlignVCenter)
        self.stop_button = small_button("停下", icon="stop")
        self.stop_button.setToolTip("不要它接着写了（已经写出来的留着）")
        self.stop_button.clicked.connect(self.stop.emit)
        self.stop_button.hide()
        bar.addWidget(self.stop_button, 0, Qt.AlignVCenter)
        bar.addStretch(1)
        self.send_button = _SendButton()
        self.send_button.clicked.connect(self.send.emit)
        bar.addWidget(self.send_button, 0, Qt.AlignVCenter)
        v.addLayout(bar)
        self._fade = QGraphicsOpacityEffect(self)
        self._fade.setOpacity(1.0)
        self._fade.setEnabled(False)
        self.setGraphicsEffect(self._fade)
        self._fit()
        self._sync()

    # ── 状态 ─────────────────────────────────────────────
    def text(self) -> str:
        return self.input.toPlainText()

    def clear(self) -> None:
        self.input.clear_text()

    def has_quote(self) -> bool:
        return self.quote_box is not None

    def set_quote(self, quote: Quote | None) -> None:
        if self.quote_box is not None:
            self.quote_slot.removeWidget(self.quote_box)
            self.quote_box.hide()
            self.quote_box.deleteLater()
            self.quote_box = None
        if quote is not None:
            self.quote_box = QuoteBox(quote, removable=True)
            self.quote_box.removed.connect(self.quote_removed.emit)
            self.quote_slot.addWidget(self.quote_box)
        self.quote_slot.setContentsMargins(0, 0, 0, 7 if quote is not None else 0)
        self._sync()

    def set_locked(self, locked: bool, placeholder: str) -> None:
        """停用（小测、没登录）：草稿先收起来，解锁后放回去；写着为什么停用。"""
        if locked and not self._locked:
            self._draft = self.text()
            self.input.blockSignals(True)
            self.clear()
            self.input.blockSignals(False)
        elif not locked and self._locked and self._draft:
            self.input.blockSignals(True)
            self.input.setPlainText(self._draft)
            self.input.reset_format()
            self.input.blockSignals(False)
            self._draft = ""
        self._locked = locked
        self._placeholder = placeholder
        self.input.setEnabled(not locked)
        self._fade.setOpacity(0.6)
        self._fade.setEnabled(locked)
        self._sync()

    def set_streaming(self, on: bool) -> None:
        self._streaming = on
        self.stop_button.setText("停下")
        self.stop_button.setEnabled(True)
        self._sync()

    def stopping(self) -> None:
        self.stop_button.setText("在停…")
        self.stop_button.setEnabled(False)

    def can_send(self) -> bool:
        return not self._locked and not self._streaming and (bool(self.text().strip()) or self.has_quote())

    def _sync(self) -> None:
        ph = self._placeholder
        if not self._locked and self.has_quote() and ph == PH_DEFAULT:
            ph = PH_QUOTE
        self.input.setPlaceholderText(ph)
        self.kbd.setVisible(not self._streaming)
        self.stop_button.setVisible(self._streaming)
        self.send_button.setEnabled(self.can_send())
        self.send_button.update()

    def _changed(self) -> None:
        self._fit()
        self._sync()
        self.typed.emit()

    def _fit(self) -> None:
        h = self.input.document().size().height()
        self.input.setFixedHeight(int(max(42, min(144, h + 2))))

    def eventFilter(self, obj, e) -> bool:
        if obj is self.input and e.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.setProperty("focus", e.type() == QEvent.FocusIn)
            repolish(self)
        return super().eventFilter(obj, e)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit()


# ═══════════════════════════════════════════════════════════
# 没登录（界面稿板 11 的意思，放在左栏里）
# ═══════════════════════════════════════════════════════════

class LoginNote(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("login")
        self.setAttribute(Qt.WA_StyledBackground, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.bang = QLabel("!", objectName="bang")
        self.bang.setFixedSize(18, 18)
        self.bang.setAlignment(Qt.AlignCenter)
        self.bang.setFont(rich_text.font(11, 700))
        head.addWidget(self.bang, 0, Qt.AlignVCenter)
        title = QLabel("AI 还没登录", objectName="loginTitle")
        title.setFont(rich_text.font(15, 600))
        head.addWidget(title, 1)
        v.addLayout(head)
        self.body = rich_label()
        v.addWidget(self.body)
        self.tip = QLabel(objectName="loginTip")
        self.tip.setWordWrap(True)
        self.tip.setFont(rich_text.font(13))
        self.tip.setText("书照常能读，只是这次问不了 AI、没有小节小测。")
        v.addWidget(self.tip)
        self.rerender()

    def rerender(self) -> None:
        self.body.setText(rich_text.md_to_html(
            "左边的 AI 用的是 reader **专用的** Claude 账号，还没登录。\n"
            "这次读完回到书架，点「打开登录窗口」登一次，以后就不用管了。",
            13, 1.7, tone="t2", strong="t1"))


# ═══════════════════════════════════════════════════════════
# 面板
# ═══════════════════════════════════════════════════════════

PANEL = """
QWidget#msgs {{ background:{bg2}; }}
QScrollArea {{ background:{bg2}; border:none; }}
QLabel {{ background:transparent; }}
QLabel#title {{ color:{t1}; }}
QLabel#sub {{ color:{t4}; }}
QLabel#cap {{ color:{t4}; }}
QLabel#hint {{ color:{t4}; }}
QLabel#qhd {{ color:{t4}; }}
QFrame#me {{ background:{bg4}; border:none; border-top-left-radius:12px; border-top-right-radius:12px;
             border-bottom-left-radius:12px; border-bottom-right-radius:4px; }}
QFrame#quoteMe {{ background:{me_quote}; border:1px solid {l2}; border-radius:9px; }}
QFrame#quoteIn {{ background:{bg3}; border:1px solid {l2}; border-radius:9px; }}
QWidget#err {{ background:{bg3}; border:1px solid {err_b}; border-radius:9px; }}
QFrame#composer {{ background:{bg0}; border:1px solid {l2}; border-radius:12px; }}
QFrame#composer[focus="true"] {{ border-color:{focus}; }}
QTextEdit#input {{ background:transparent; border:none; color:{t1}; selection-background-color:{sel};
                   selection-color:{t1}; }}
QLabel#kbd {{ color:{t4}; border:1px solid {l2}; border-radius:4px; padding:0 5px; min-height:17px;
              max-height:19px; }}
QFrame#login {{ background:{bg3}; border:1px solid {err_b}; border-radius:12px; }}
QLabel#bang {{ background:{red}; color:{acc_ink}; border-radius:9px; }}
QLabel#loginTitle {{ color:{t1}; }}
QLabel#loginTip {{ background:{bg4}; color:{t2}; border-radius:8px; padding:9px 11px; }}
""" + BUTTONS


class AiPanel(QWidget):
    """左栏。接口见 docs/UI_PLAN.md「C」。"""

    activity = Signal()
    jump_to = Signal(object, str)
    quiz_finished = Signal(int, bool)
    collapsed_changed = Signal(bool)
    next_section = Signal()             # 小测卡「接着读下一节 ›」（多出来的，读书界面接上就能帮主人翻页）
    quiz_skipped = Signal(int)          # 小测卡「跳过这题」（小节号）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("aiPanel")
        self._companion: Companion | None = None
        self._loc: Loc | None = None
        self._context = ""
        self._quote: Quote | None = None
        self._collapsed = False
        self._gen = 0
        self._thread: AskThread | None = None
        self._reply: Reply | None = None
        self._card: QuizCard | None = None
        self._items: list[QWidget] = []
        self._stick = True
        self._auto_scroll = False
        self._login_note: LoginNote | None = None
        self._src_card: QuizCard | None = None
        self._src_timer = QTimer(self)                # 跳过去 1.5 秒内没报位置就算了
        self._src_timer.setSingleShot(True)
        self._src_timer.setInterval(1500)
        self._src_timer.timeout.connect(lambda: setattr(self, "_src_card", None))
        self.setFixedWidth(WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        restyle(self, PANEL)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 1, 0)          # 右边 1px 画分隔线
        root.setSpacing(0)

        # 顶上
        self.head = QWidget()
        self.head.setFixedHeight(54)
        hh = QHBoxLayout(self.head)
        hh.setContentsMargins(18, 0, 12, 0)
        hh.setSpacing(8)
        tv = QVBoxLayout()
        tv.setSpacing(1)
        tv.setContentsMargins(0, 0, 0, 0)
        tr = QHBoxLayout()
        tr.setSpacing(7)
        self.title = QLabel("问 AI", objectName="title")
        self.title.setFont(rich_text.font(14, 600))
        tr.addWidget(self.title)
        self.dot = _Dot("acc", 7)
        self.dot.setToolTip("有一道小测没答完")
        self.dot.hide()
        tr.addWidget(self.dot, 0, Qt.AlignVCenter)
        tr.addStretch(1)
        tv.addStretch(1)
        tv.addLayout(tr)
        self.sub = QLabel("", objectName="sub")
        self.sub.setFont(rich_text.font(11.5))
        self.sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tv.addWidget(self.sub)
        tv.addStretch(1)
        hh.addLayout(tv, 1)
        self.fold = IconButton("fold", "收起（书占满）")
        self.fold.clicked.connect(lambda: self.set_collapsed(True))
        hh.addWidget(self.fold, 0, Qt.AlignVCenter)
        root.addWidget(self.head)

        # 收起后的一条
        self.rail = QWidget()
        rv = QVBoxLayout(self.rail)
        rv.setContentsMargins(0, 13, 0, 0)
        rv.setSpacing(10)
        self.unfold = IconButton("unfold", "展开「问 AI」")
        self.unfold.clicked.connect(lambda: self.set_collapsed(False))
        rv.addWidget(self.unfold, 0, Qt.AlignHCenter)
        self.rail_dot = _Dot("acc", 7)
        self.rail_dot.setToolTip("有一道小测没答完")
        self.rail_dot.hide()
        rv.addWidget(self.rail_dot, 0, Qt.AlignHCenter)
        rv.addStretch(1)
        self.rail.hide()
        root.addWidget(self.rail, 1)

        # 消息
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.msgs = QWidget(objectName="msgs")
        self.list = QVBoxLayout(self.msgs)
        self.list.setContentsMargins(18, 4, 18, 12)
        self.list.setSpacing(16)
        self.list.addStretch(1)
        self.hint = QLabel(objectName="hint")
        self.hint.setWordWrap(True)
        self.hint.setFont(rich_text.font(13))
        self.hint.setText("在书上选一段，点「问 AI」，那段话会带到下面的输入框。\n"
                          "也可以直接问：这一节在讲什么、某个词是什么意思。")
        self.list.addWidget(self.hint)
        self.scroll.setWidget(self.msgs)
        sb = self.scroll.verticalScrollBar()
        sb.rangeChanged.connect(self._range_changed)
        sb.valueChanged.connect(self._scrolled)
        root.addWidget(self.scroll, 1)

        # 输入
        self.composer = Composer()
        self.composer.send.connect(self._send)
        self.composer.stop.connect(self.stop)
        self.composer.typed.connect(self.activity.emit)
        self.composer.quote_removed.connect(lambda: self._set_quote(None))
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(14, 0, 14, 14)
        wl.addWidget(self.composer)
        self.composer_wrap = wrap
        root.addWidget(wrap)

        theme.on_change(self._on_theme)
        self._apply_locks()

    # ══ 接口 ═════════════════════════════════════════════
    def set_companion(self, companion: Companion | None) -> None:
        """None = 没登录：显示说明，输入框停用（书照常能读）。换了伙伴 = 新的一次读书，清空对话。"""
        if companion is self._companion and companion is not None:
            return
        self.stop()
        self._gen += 1                   # 旧线程的结果不要了
        self._detach_thread()
        self._clear_messages()
        self._companion = companion
        if companion is None:
            self._login_note = LoginNote()
            self._add(self._login_note)
        self._apply_locks()
        self._update_sub()

    def set_context(self, loc: Loc, label: str) -> None:
        self._loc = loc
        self._context = label
        self._update_sub()
        card, self._src_card = self._src_card, None
        if card is not None:
            pages = re.findall(r"第\s*\d+\s*页", label)
            if pages:
                try:
                    card.set_source_page(pages[-1])
                except RuntimeError:
                    pass

    def attach_quote(self, text: str, loc: Loc) -> None:
        """书上点了「问 AI」：引用进输入框，焦点进输入框（是主人自己点的）。"""
        if not text.strip():
            return
        self.set_collapsed(False)
        self._set_quote(Quote(text.strip(), loc, self._quote_label(loc)))
        if self._companion is not None and self._card_pending() is None:
            self.composer.input.setFocus(Qt.OtherFocusReason)
        self.activity.emit()

    def show_quiz(self, quiz: Quiz) -> None:
        """出题：卡放进对话里，输入框停用，顶上亮小点。不拿焦点、不出声。"""
        old = self._card_pending()
        if old is not None:
            self._remove(old)
        if self._collapsed:
            self.set_collapsed(False)      # 出题了得让主人看见（仍然不拿焦点）
        title = ""
        page_label = None
        if self._companion is not None:
            book = self._companion.book
            if 0 <= quiz.section < len(book.sections):
                title = book.sections[quiz.section].title
            if book.kind == "pdf":
                page_label = lambda loc: f"第 {loc.doc + 1} 页"  # noqa: E731
        card = QuizCard(quiz, title, page_label)
        card.wrong.connect(lambda loc, quote, c=card: self._jump(c, loc, quote))
        card.finished.connect(self._quiz_done)
        card.next_section.connect(self._continue)
        card.skipped.connect(lambda s, c=card: self._skipped(c, s))
        card.activity.connect(self.activity.emit)
        self._card = card
        self._add(card)
        self._stick = True
        self._apply_locks()
        self._dim()

    def quiz_timeout_note(self) -> None:
        card = self._card_pending()
        if card is None:
            return
        card.timeout_note()
        self._apply_locks()             # 答不答都行：不再锁输入框
        self._dim()

    def stop(self) -> None:
        if self._thread is not None and self._companion is not None:
            self._companion.stop.set()
            self.composer.stopping()

    # ── 多出来的（读书界面可以用，也可以不用） ───────────
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, on: bool) -> None:
        if on == self._collapsed:
            return
        self._collapsed = on
        self.head.setVisible(not on)
        self.scroll.setVisible(not on)
        self.composer_wrap.setVisible(not on)
        self.rail.setVisible(on)
        self.setFixedWidth(RAIL if on else WIDTH)
        self.collapsed_changed.emit(on)

    def is_streaming(self) -> bool:
        return self._thread is not None

    # ══ 发问 ═════════════════════════════════════════════
    def _send(self) -> None:
        if not self.composer.can_send() or self._companion is None:
            return
        typed = self.composer.text().strip()
        quote = self._quote
        question = typed or ASK_EXPLAIN
        loc = quote.loc if quote else (self._loc or Loc(0, 0))
        if self._card is not None:
            # 题答完了又问别的 / 题没答就问别的（题不挡人，放一边也行）：卡不再是「当前」的，留在对话里还能答
            self._card = None
        self._add(BubbleRow(Bubble(question, quote)))
        self.composer.clear()
        self._set_quote(None)
        self._stick = True
        reply = Reply(question, loc, quote.text if quote else "")
        reply.retry.connect(lambda r=reply: self._retry(r))
        self._add(reply)
        self._start(reply)
        self.activity.emit()

    def _retry(self, reply: Reply) -> None:
        if self._thread is not None or self._companion is None:
            return
        self._stick = True
        self._start(reply)
        self.activity.emit()

    def _start(self, reply: Reply) -> None:
        self._gen += 1
        self._reply = reply
        reply.begin()
        th = AskThread(self._gen, self._companion, reply.question, reply.loc, reply.selection)
        th.delta.connect(self._on_delta)
        th.done.connect(self._on_done)
        th.finished.connect(lambda t=th: _LIVE_THREADS.discard(t))
        th.finished.connect(th.deleteLater)
        _LIVE_THREADS.add(th)
        self._thread = th
        self.composer.set_streaming(True)
        self._set_retry_enabled(False)
        self._dim()
        th.start()

    def _on_delta(self, gen: int, text: str) -> None:
        if gen != self._gen or self._reply is None:
            return
        self._reply.feed(text)

    def _on_done(self, gen: int, result: Result) -> None:
        if gen != self._gen:
            return
        reply, self._reply = self._reply, None
        self._thread = None
        self.composer.set_streaming(False)
        if reply is not None:
            reply.finish(result)
        self._set_retry_enabled(True)
        self._apply_locks()
        self._dim()

    def _detach_thread(self) -> None:
        self._thread = None
        self._reply = None
        self.composer.set_streaming(False)

    def _set_retry_enabled(self, on: bool) -> None:
        for w in self._items:
            if isinstance(w, Reply):
                w.set_retry_enabled(on)

    # ══ 小测 ═════════════════════════════════════════════
    def _card_pending(self) -> QuizCard | None:
        """还没答完、而且时间没到的题（输入框要停用）。"""
        c = self._card
        return c if c is not None and not c.done and not c.timed_out else None

    def _jump(self, card: QuizCard, loc: Loc, quote: str) -> None:
        """答错 → 书页跳到出处。跳过去后书页报回来的第一个位置就是出处那页，记到卡上。"""
        self._src_card = card
        self._src_timer.start()
        self.jump_to.emit(loc, quote)

    def _continue(self) -> None:
        """「接着读下一节 ›」：卡留在对话里，之前的对话不再淡着。"""
        self._card = None
        self._dim()
        self.next_section.emit()

    def _skipped(self, card: QuizCard, section: int) -> None:
        """「跳过这题」：卡留在对话里（写着跳过了），不再是当前的，输入框、小点照常。"""
        if self._card is card:
            self._card = None
        self._apply_locks()
        self._dim()
        self.quiz_skipped.emit(section)

    def _quiz_done(self, section: int, ok: bool) -> None:
        # 答完：输入框马上能用；卡还是「当前」的，直到主人接着读或者又问了别的（界面稿板 5「答对」）
        self._apply_locks()
        self._dim()
        self.quiz_finished.emit(section, ok)

    # ══ 状态 ═════════════════════════════════════════════
    def _apply_locks(self) -> None:
        pending = self._card_pending() is not None
        if self._companion is None:
            self.composer.set_locked(True, PH_NOLOGIN)
        elif pending:
            self.composer.set_locked(False, PH_QUIZ)     # 有题也能问（2026-09-25 起：有题也不拦着主人提问）
        else:
            self.composer.set_locked(False, PH_DEFAULT)
        self.dot.setVisible(pending)
        self.rail_dot.setVisible(pending)
        self.hint.setVisible(self._companion is not None and not any(
            isinstance(w, (BubbleRow, QuizCard)) for w in self._items))

    def _dim(self) -> None:
        """有题：之前的对话淡到 .35；正在回答：之前的问答淡到 .5（界面稿板 4③、板 5）。"""
        card = self._card
        current: set[int] = set()
        level = 1.0
        if card is not None:
            level = 0.35
            current = {id(card)}
            if self._reply is not None:
                current.add(id(self._reply))
        elif self._reply is not None:
            level = 0.5
            i = self._items.index(self._reply) if self._reply in self._items else len(self._items)
            current = {id(w) for w in self._items[max(0, i - 1):i + 1]}   # 这一问和它的回答
        for w in self._items:
            if isinstance(w, LoginNote):
                continue
            op = 1.0 if id(w) in current else level
            eff = w.graphicsEffect()
            if op >= 1.0:
                if eff is not None:
                    w.setGraphicsEffect(None)
            else:
                if not isinstance(eff, QGraphicsOpacityEffect):
                    eff = QGraphicsOpacityEffect(w)
                    w.setGraphicsEffect(eff)
                eff.setOpacity(op)

    def _update_sub(self) -> None:
        if self._companion is None:
            text = "AI 没登录 · 书照常能读"
        else:
            text = f"看得到：{self._context}" if self._context else "看得到：这一节"
        fm = self.sub.fontMetrics()
        self.sub.setText(fm.elidedText(text, Qt.ElideRight, WIDTH - 18 - 12 - 28 - 16))
        self.sub.setToolTip(text if self.sub.text() != text else "")

    def _quote_label(self, loc: Loc) -> str:
        if self._companion is not None and self._companion.book.kind == "pdf":
            return f"引用 · 第 {loc.doc + 1} 页"
        pages = re.findall(r"第\s*\d+\s*页", self._context)
        return f"引用 · {pages[-1]}" if pages else "引用"

    def _set_quote(self, quote: Quote | None) -> None:
        self._quote = quote
        self.composer.set_quote(quote)

    # ══ 消息列表 ═════════════════════════════════════════
    def _add(self, w: QWidget) -> None:
        self._items.append(w)
        self.list.insertWidget(self.list.count() - 1, w)      # 提示那一行永远在最后
        self._apply_locks()

    def _remove(self, w: QWidget) -> None:
        if w in self._items:
            self._items.remove(w)
        self.list.removeWidget(w)
        w.hide()
        w.deleteLater()
        if w is self._card:
            self._card = None

    def _clear_messages(self) -> None:
        for w in list(self._items):
            self._remove(w)
        self._card = None
        self._login_note = None
        self._set_quote(None)

    def _range_changed(self, _lo: int, hi: int) -> None:
        if self._stick:
            self._auto_scroll = True
            self.scroll.verticalScrollBar().setValue(hi)
            self._auto_scroll = False

    def _scrolled(self, value: int) -> None:
        if self._auto_scroll:
            return
        sb = self.scroll.verticalScrollBar()
        self._stick = value >= sb.maximum() - 24         # 主人往上翻了就不再自动滚到底
        self.activity.emit()                              # 翻看回答也算在读

    def _on_theme(self, _name: str = "") -> None:
        for w in self._items:
            if hasattr(w, "rerender"):
                w.rerender()
        self.composer.set_quote(self._quote)
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.color("bg2"))
        p.fillRect(self.width() - 1, 0, 1, self.height(), theme.color("l1"))
        p.end()


# ═══════════════════════════════════════════════════════════
# 演示：python -m reader.ui.ai_panel [书的路径] [--fake] [--light]
# ═══════════════════════════════════════════════════════════

def _demo() -> int:
    import sys
    import threading
    import time
    from pathlib import Path

    theme.prepare_platform()
    from PySide6.QtWidgets import QApplication, QPushButton

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fake = "--fake" in sys.argv
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.set_theme("light" if "--light" in sys.argv else theme.saved_choice(), save=False)

    from reader.books import open_book
    path = Path(args[0]) if args else next(iter(sorted(Path.home().glob("Downloads/*.epub"))), None)
    if path is None:
        print("找不到书：python -m reader.ui.ai_panel <书的路径>")
        return 1
    book = open_book(path)
    sec = book.sections[min(2, len(book.sections) - 1)]

    def fake_run(prompt, *, system=None, resume=None, stop=None, **kw):
        text = ("**先说结论**：这一节讲的是惩罚的对象从身体换成了「灵魂」。\n"
                "作者用一场公开处决开头，是为了跟八十年后的监狱作息表对照。\n\n"
                "1. 公开处决：惩罚是一场给人看的戏\n2. 作息表：惩罚变成了看不见的程序\n\n"
                "这是书外的补充：同一时期欧洲各国都在改刑法。")
        out = []
        for ch in text:
            if stop is not None and stop.is_set():
                yield Result(text="".join(out), error="已停止")
                return
            out.append(ch)
            yield Delta(ch)
            time.sleep(0.02)
        yield Result(text=text, session_id="demo")

    companion = Companion(book, runner=fake_run) if fake else None

    win = QWidget()
    win.setWindowTitle(f"AI 栏演示 · {book.title}")
    h = QHBoxLayout(win)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(0)
    panel = AiPanel()
    h.addWidget(panel)
    side = QWidget()
    theme.style(side, "QWidget {{ background: {paper}; color: {ink}; }}")
    sv = QVBoxLayout(side)
    sv.setContentsMargins(24, 24, 24, 24)
    info = QLabel(f"《{book.title}》\n{sec.title}\n（右边是演示用的按钮，不是书页）")
    info.setWordWrap(True)
    sv.addWidget(info)
    log = QLabel("")
    log.setWordWrap(True)

    def say(s: str) -> None:
        log.setText(s)

    panel.jump_to.connect(lambda loc, q: say(f"jump_to {loc}：{q[:30]}"))
    panel.quiz_finished.connect(lambda s, ok: say(f"quiz_finished 第 {s} 节，{'对' if ok else '错'}"))
    panel.next_section.connect(lambda: say("next_section"))
    panel.collapsed_changed.connect(lambda c: say(f"collapsed {c}"))
    body = [ln.strip() for ln in sec.text().split("\n") if len(ln.strip()) > 40]
    passage = body[len(body) // 3][:80] if body else sec.title
    panel.set_context(sec.start, f"{sec.title} · 第 34 页")

    def btn(text, fn):
        b = QPushButton(text)
        b.clicked.connect(fn)
        sv.addWidget(b)

    def quote():
        panel.attach_quote(passage, sec.start)

    def quiz():
        from reader.ai.companion import locate_quote
        q = Quiz(sec.idx, "按这一节的说法，公开处决为什么渐渐消失了？",
                 ["因为处决花钱太多", "因为人们觉得它和犯罪一样野蛮", "因为没人再去围观", "因为法律明文禁止"],
                 1, ["书里没提钱，回原文看看作者怎么说观众的感受。", "对。", "恰恰相反，观众很多。", "不是法律的原因。"],
                 "原文说，这种惩罚方式的野蛮程度不亚于犯罪本身。", passage)
        q.quote_loc = locate_quote(book, sec, passage)
        panel.show_quiz(q)

    btn("书上选一段点「问 AI」", quote)
    btn("出一道题（假题）", quiz)
    btn("时间到了", panel.quiz_timeout_note)
    btn("没登录", lambda: panel.set_companion(None))
    btn("换主题", lambda: theme.set_theme("light" if theme.current() == "dark" else "dark", save=False))
    sv.addWidget(log)
    sv.addStretch(1)
    h.addWidget(side, 1)
    win.resize(900, 820)
    win.show()
    theme.titlebar(win)

    if fake:
        panel.set_companion(companion)
    else:
        # 查一下专用账号登录没有（几秒钟，不卡界面）
        panel.set_context(sec.start, f"{sec.title} · 第 34 页")
        say("在查专用账号登录没有…")
        box: dict = {}

        def check():
            from reader.ai import claude
            box["st"] = claude.auth_status()
        t = threading.Thread(target=check, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                QTimer.singleShot(200, poll)
                return
            ok = bool(box.get("st", {}).get("loggedIn"))
            panel.set_companion(Companion(book) if ok else None)
            panel.set_context(sec.start, f"{sec.title} · 第 34 页")
            say("已登录，用真 AI" if ok else f"没登录：{box.get('st')}")
        poll()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_demo())
