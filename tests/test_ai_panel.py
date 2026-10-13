"""AI 栏（左栏）+ 小测卡 + Markdown 排版的离线测试。不调真 AI（假 runner），不开真窗口。

跑法：python -m unittest discover -s tests
（自己设 QT_QPA_PLATFORM=offscreen；已经设了别的就照用。）
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QWidget  # noqa: E402

try:
    from tests.test_books_ai import LONG, make_epub
except ImportError:                                     # discover -s tests 时按顶层模块导入
    from test_books_ai import LONG, make_epub

from reader.ai.claude import LOGIN_HINT, Delta, Result  # noqa: E402
from reader.ai.companion import Companion, Quiz  # noqa: E402
from reader.books import Loc, open_book  # noqa: E402
from reader.ui import rich_text, theme  # noqa: E402

_app = QApplication.instance() or QApplication([])
theme.load_fonts()

from reader.ui.ai_panel import (ASK_EXPLAIN, PH_NOLOGIN, PH_QUIZ, AiPanel, BubbleRow,  # noqa: E402
                                LoginNote, Reply, friendly_error)
from reader.ui.quiz_card import QuizCard  # noqa: E402


def pump(seconds: float = 0.05) -> None:
    end = time.time() + seconds
    while time.time() < end:
        _app.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        _app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


class Runner:
    """假 runner：按顺序吐预设回答（一个字一个 Delta），能出错、能被停下、能卡在半路。"""

    def __init__(self, answers=(), errors=(), slow: float = 0.0, gate: threading.Event | None = None):
        self.answers = list(answers)
        self.errors = list(errors)          # 第 i 次调用的错误（None = 不出错）
        self.slow = slow
        self.gate = gate
        self.calls: list[dict] = []

    def run(self, prompt, *, system=None, resume=None, stop=None, **kw):
        self.calls.append({"prompt": prompt, "system": system, "resume": resume})
        err = self.errors.pop(0) if self.errors else None
        if err:
            yield Result(error=err)
            return
        text = self.answers.pop(0) if self.answers else "好的"
        out = []
        for i, ch in enumerate(text):
            if stop is not None and stop.is_set():
                yield Result(text="".join(out), error="已停止")
                return
            if self.gate is not None and i == len(text) // 2:
                self.gate.wait(5)
            out.append(ch)
            yield Delta(ch)
            if self.slow:
                time.sleep(self.slow)
        yield Result(text=text, session_id="s1")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp())
        cls.book = open_book(make_epub(tmp / "a.epub", [
            ("第一章 酷刑", [LONG] * 5 + ["在人们看来，这种惩罚方式，其野蛮程度不亚于，甚至超过犯罪本身。"]),
            ("第二章 监狱", [LONG] * 5)]))

    def setUp(self):
        theme.set_theme("dark", save=False)
        self.panel = AiPanel()
        self.panel.resize(340, 800)
        self.panel.setFixedHeight(800)

    def tearDown(self):
        try:
            wait_until(lambda: not self.panel.is_streaming(), 5)
            self.panel.close()
            self.panel.deleteLater()
        except RuntimeError:          # 跟着测试里的窗口一起没了
            pass
        pump(0.02)

    def companion(self, runner: Runner) -> Companion:
        c = Companion(self.book, runner=runner.run)
        self.panel.set_companion(c)
        self.panel.set_context(Loc(0, 1), "第一章 酷刑 · 第 3 页")
        self.panel.show()
        pump(0.02)
        return c

    def send(self, text: str) -> None:
        self.panel.composer.input.setPlainText(text)
        self.panel._send()

    def replies(self) -> list[Reply]:
        return [w for w in self.panel._items if isinstance(w, Reply)]

    def quiz(self, section: int = 0) -> Quiz:
        return Quiz(section, "为什么公开处决被排斥？", ["太花钱", "被看得和犯罪一样野蛮", "没人来看", "法律禁止"],
                    1, ["书里没提钱，回去看第一段", "对", "观众其实很多", "不是法律原因"],
                    "原文说它的野蛮程度不亚于犯罪。", "其野蛮程度不亚于", Loc(0, 6))


# ═══════════════════════════════════════════════════════════
# Markdown → HTML
# ═══════════════════════════════════════════════════════════

class RichTextTests(unittest.TestCase):
    def test_single_newline_kept_and_paragraph_gap(self):
        h = rich_text.md_to_html("第一行\n第二行\n\n第二段")
        self.assertIn("<br />", h)                                    # 单个换行没被吞
        self.assertIn("第一行<br />第二行", h)
        paras = re.findall(r"<p style=\"([^\"]*)\">", h)
        self.assertEqual(len(paras), 2)                               # 空行才分段
        self.assertIn(f"margin-bottom:{rich_text.PARA_GAP}px", paras[0])
        self.assertIn("margin-bottom:0px", paras[1])

    def test_paragraph_gap_larger_than_line_gap(self):
        px, css = 14, 1.78
        self.assertGreater(rich_text.PARA_GAP, 0)
        # 行高换算：Qt 的比例行高乘在字体自然行距上（MiSans 约 1.33）
        pct = rich_text.qt_line_height(px, css)
        from PySide6.QtGui import QFontMetricsF
        natural = QFontMetricsF(rich_text.font(px)).lineSpacing()
        self.assertAlmostEqual(pct / 100 * natural, css * px, delta=1.0)
        self.assertIn(f"line-height:{pct}%", rich_text.md_to_html("字", px, css))
        # 段距 = 行间空隙 + PARA_GAP，一定比行间空隙大
        line_gap = css * px - px
        self.assertGreater(line_gap + rich_text.PARA_GAP, line_gap)

    def test_bold_capped_at_600(self):
        h = rich_text.md_to_html("**加粗** 和 # 标题\n\n# 标题")
        weights = [int(w) for w in re.findall(r"font-weight:(\d+)", h)]
        self.assertTrue(weights)
        self.assertLessEqual(max(weights), 600)
        self.assertIn("font-weight:600", h)

    def test_chinese_bold_after_punctuation(self):
        """CommonMark 不认「。**后面」这种收尾（真 AI 回答里常见），要自己认。"""
        for text, bold in [("**结论：先说。**分尸写得细", "结论：先说。"),
                           ("- **刑罚变温和了。**他说", "刑罚变温和了。"),
                           ("甲 **乙** 丙", "乙")]:
            h = rich_text.md_to_html(text)
            self.assertNotIn("**", h, text)
            self.assertRegex(h, r'font-weight:600;[^>]*>' + re.escape(bold) + "<", text)
        h = rich_text.md_to_html("代码 `**x**` 不动")
        self.assertIn("**x**", h)

    def test_colors_from_theme(self):
        theme.set_theme("dark", save=False)
        h = rich_text.md_to_html("正文 **重点**", tone="t2", strong="t1")
        self.assertIn(theme.tok("t2").lower(), h.lower())
        self.assertIn(theme.tok("t1").lower(), h.lower())
        theme.set_theme("light", save=False)
        h2 = rich_text.md_to_html("正文 **重点**", tone="t2", strong="t1")
        self.assertIn(theme.tok("t2", "light").lower(), h2.lower())
        theme.set_theme("dark", save=False)

    def test_no_html_injection_and_lists(self):
        h = rich_text.md_to_html("a<b 和 <i>斜</i>\n\n2. 第二\n3. 第三\n\n- 点")
        self.assertIn("a&lt;b", h)
        self.assertIn("&lt;i&gt;", h)                    # 回答里的 HTML 标签原样显示，不生效
        self.assertIn("2. ", h)                          # 编号从 2 开始，照原文
        self.assertIn("3. ", h)
        self.assertIn("• ", h)
        self.assertNotIn("<ol", h)
        self.assertNotIn("<ul", h)

    def test_code_fence_keeps_newlines(self):
        pre = rich_text.prepare_markdown("```\na\nb\n```\n行一\n行二")
        self.assertIn("a\nb", pre)
        self.assertIn("行一" + rich_text.LINE_SEP + "行二", pre)

    def test_split_stable(self):
        s = rich_text.split_stable
        self.assertEqual(s("第一段\n\n第二段还在写"), len("第一段\n\n"))
        self.assertEqual(s("第一段\n\n"), 0)                       # 空行后面还没字：不切
        self.assertEqual(s("只有一段"), 0)
        self.assertEqual(s("```\ncode\n\nmore\n"), 0)               # 代码块里的空行不切
        self.assertEqual(s("```\nx\n```\n\n后面"), len("```\nx\n```\n\n"))

    def test_cursor(self):
        h = rich_text.md_to_html("字", cursor="acc")
        self.assertIn(f"background-color:{theme.tok('acc').lower()}", h.lower())

    def test_plain_html_keeps_lines(self):
        h = rich_text.plain_html("一\n二\n\n三")
        self.assertIn("一<br />二", h)
        self.assertEqual(len(re.findall(r"<p ", h)), 2)


# ═══════════════════════════════════════════════════════════
# 对话：流式、停下、出错重试、输入框
# ═══════════════════════════════════════════════════════════

class ChatTests(Base):
    def test_streaming_appends_incrementally(self):
        a1 = "第一段回答。\n\n第二段回答。"
        a2 = "第一段。\n第一段的第二行。\n\n第二段。\n\n第三段，最后。"
        r = Runner([a1, a2], slow=0.003)
        self.companion(r)
        self.send("这章讲什么？")
        self.assertTrue(self.panel.is_streaming())
        self.assertTrue(self.panel.composer.stop_button.isVisibleTo(self.panel))
        self.assertFalse(self.panel.composer.kbd.isVisibleTo(self.panel))
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        first = self.replies()[0]
        self.assertEqual(first.buffer, a1)
        self.assertIsNone(first.error)
        renders_before = first.renders
        chunk_labels = [lab for _t, lab in first.chunks]

        self.send("再说说？")
        second = self.replies()[1]
        seen: list[int] = []
        while self.panel.is_streaming():
            _app.processEvents()
            seen.append(len(second.buffer))
            time.sleep(0.002)
        pump(0.1)
        self.assertEqual(second.buffer, a2)
        self.assertGreater(len(set(seen)), 2)                        # 真是一段段到的
        self.assertEqual(first.renders, renders_before)              # 之前的回答一次都没重排
        self.assertEqual([lab for _t, lab in first.chunks], chunk_labels)
        # 写完的段落定下来（切在空行处），之后不再重排；最后还在写的那截在 tail 里
        pieces = [t for t, _l in second.chunks]
        self.assertTrue(pieces)
        self.assertEqual("\n\n".join(pieces + [a2[second.frozen:].strip("\n")]), a2)
        self.assertTrue(a2[:second.frozen].endswith("\n\n"))
        self.assertLess(second.renders, len(a2))                     # 攒着排（不是一个字排一次）
        self.assertIn("第三段", second.tail.text())
        self.assertNotIn(theme.tok("acc").lower(), second.tail.text().lower())  # 写完了光标没了
        self.assertFalse(self.panel.composer.stop_button.isVisibleTo(self.panel))
        self.assertEqual(len(r.calls), 2)
        self.assertEqual(r.calls[1]["resume"], "s1")

    def test_stop(self):
        gate = threading.Event()
        r = Runner(["一二三四五六七八九十" * 5], gate=gate)
        c = self.companion(r)
        self.send("讲讲")
        self.assertTrue(wait_until(lambda: len(self.replies()[0].buffer) > 0))
        self.panel.stop()
        self.assertTrue(c.stop.is_set())
        self.assertFalse(self.panel.composer.stop_button.isEnabled())   # 「在停…」
        gate.set()
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        pump(0.05)
        rep = self.replies()[0]
        self.assertEqual(rep.error, "已停止")
        self.assertTrue(rep.buffer)                                     # 写出来的留着
        self.assertLess(len(rep.buffer), 50)
        self.assertFalse(rep.status.isHidden())
        self.assertIsNone(getattr(rep, "retry_button", None))          # 自己停的：不给「重试」
        self.assertTrue(self.panel.composer.kbd.isVisibleTo(self.panel))

    def test_error_then_retry_in_place(self):
        r = Runner(["重试后的回答"], errors=["Claude 超时（240 秒）"])
        self.companion(r)
        self.send("讲讲")
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        pump(0.05)
        rep = self.replies()[0]
        self.assertEqual(rep.error, "Claude 超时（240 秒）")
        self.assertIn("超时", rep.err_label.text())
        self.assertTrue(rep.retry_button.isEnabled())
        n_items = len(self.panel._items)
        rep.retry_button.click()
        self.assertTrue(self.panel.is_streaming())
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        pump(0.05)
        self.assertIsNone(rep.error)
        self.assertEqual(rep.buffer, "重试后的回答")
        self.assertTrue(rep.status.isHidden())
        self.assertEqual(len(self.panel._items), n_items)             # 原地重答，没多出一条
        self.assertEqual(r.calls[0]["prompt"], r.calls[1]["prompt"])  # 问的还是同一句

    def test_friendly_errors(self):
        self.assertIn("没登录", friendly_error(LOGIN_HINT))
        self.assertIn("超时", friendly_error("Claude 超时（240 秒）"))
        self.assertIn("额度", friendly_error("Claude AI usage limit reached"))
        self.assertTrue(friendly_error("RuntimeError: boom").startswith("没答上来"))

    def test_ctrl_enter_sends_enter_newline(self):
        r = Runner(["好"])
        self.companion(r)
        inp = self.panel.composer.input
        inp.setFocus()
        QTest.keyClicks(inp, "abc")
        QTest.keyClick(inp, Qt.Key_Return)
        self.assertEqual(inp.toPlainText(), "abc\n")
        self.assertEqual(r.calls, [])
        QTest.keyClicks(inp, "d")
        QTest.keyClick(inp, Qt.Key_Return, Qt.ControlModifier)
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        self.assertEqual(len(r.calls), 1)
        self.assertIn("【主人问】abc\nd", r.calls[0]["prompt"])
        self.assertEqual(inp.toPlainText(), "")

    def test_empty_does_not_send_and_quote_only_means_explain(self):
        r = Runner(["解释"])
        self.companion(r)
        self.assertFalse(self.panel.composer.send_button.isEnabled())
        self.panel._send()
        self.assertEqual(r.calls, [])
        self.panel.attach_quote("其野蛮程度不亚于，甚至超过犯罪本身", Loc(0, 6))
        self.assertTrue(self.panel.composer.has_quote())
        self.assertTrue(self.panel.composer.send_button.isEnabled())
        self.panel._send()
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))
        self.assertIn(f"【主人问】{ASK_EXPLAIN}", r.calls[0]["prompt"])
        self.assertIn("「其野蛮程度不亚于，甚至超过犯罪本身」", r.calls[0]["prompt"])
        self.assertFalse(self.panel.composer.has_quote())             # 发出去就清掉
        bubble = [w for w in self.panel._items if isinstance(w, BubbleRow)][0].bubble
        self.assertIsNotNone(bubble.quote_box)
        self.assertEqual(bubble.text, ASK_EXPLAIN)

    def test_quote_removable_and_focus(self):
        self.companion(Runner())
        win = QWidget()
        lay = QHBoxLayout(win)
        book = QLineEdit()
        lay.addWidget(self.panel)
        lay.addWidget(book)
        win.show()
        win.activateWindow()
        book.setFocus()
        pump(0.05)
        self.panel.attach_quote("一段话", Loc(0, 2))
        pump(0.02)
        self.assertIs(QApplication.focusWidget(), self.panel.composer.input)   # 主人自己点的：焦点进输入框
        self.assertIn("第 3 页", self.panel.composer.quote_box.quote.label)      # 页码取自「看得到」
        self.panel.composer.quote_box.removed.emit()
        pump(0.02)
        self.assertFalse(self.panel.composer.has_quote())
        win.close()

    def test_not_logged_in(self):
        self.panel.set_companion(None)
        self.panel.show()
        pump(0.02)
        self.assertTrue(any(isinstance(w, LoginNote) for w in self.panel._items))
        self.assertFalse(self.panel.composer.input.isEnabled())
        self.assertEqual(self.panel.composer.input.placeholderText(), PH_NOLOGIN)
        self.panel.composer.input.setPlainText("x")
        self.assertFalse(self.panel.composer.can_send())
        self.panel.attach_quote("一段", Loc(0, 1))                     # 不崩；也不给焦点
        self.assertNotEqual(QApplication.focusWidget(), self.panel.composer.input)
        # 登好了：说明没了，能问
        self.companion(Runner(["好"]))
        self.assertFalse(any(isinstance(w, LoginNote) for w in self.panel._items))
        self.assertTrue(self.panel.composer.input.isEnabled())

    def test_collapse(self):
        self.companion(Runner())
        got = []
        self.panel.collapsed_changed.connect(got.append)
        self.panel.fold.click()
        self.assertEqual(got, [True])
        self.assertEqual(self.panel.width(), 44)
        self.panel.show_quiz(self.quiz())                               # 出题会展开（不拿焦点）
        self.assertEqual(got, [True, False])
        self.assertEqual(self.panel.width(), 340)

    def test_context_label(self):
        self.companion(Runner())
        self.panel.set_context(Loc(0, 3), "第三章 · 3.2 节 · 第 134 页")
        # 屏幕字体较宽时标签会省略，完整位置必须仍可从提示中取得。
        self.assertIn("看得到：第三章 · 3.2 节 · 第 134 页",
                      self.panel.sub.toolTip() or self.panel.sub.text())

    def test_activity(self):
        self.companion(Runner(["好"]))
        n = []
        self.panel.activity.connect(lambda: n.append(1))
        self.panel.composer.input.setPlainText("打字")
        self.assertTrue(n)
        n.clear()
        self.panel._send()
        self.assertTrue(n)
        self.assertTrue(wait_until(lambda: not self.panel.is_streaming()))


# ═══════════════════════════════════════════════════════════
# 小测
# ═══════════════════════════════════════════════════════════

class QuizTests(Base):
    def setUp(self):
        super().setUp()
        self.companion(Runner(["好"]))
        self.jumps, self.done, self.nexts = [], [], []
        self.panel.jump_to.connect(lambda loc, q: self.jumps.append((loc, q)))
        self.panel.quiz_finished.connect(lambda s, ok: self.done.append((s, ok)))
        self.panel.next_section.connect(lambda: self.nexts.append(1))

    def card(self) -> QuizCard:
        return self.panel._card

    def test_quiz_does_not_lock_and_dot(self):
        """有题也能问（2026-09-25 起：有题也不拦着主人提问）；小点亮着，答完灭。"""
        self.panel.composer.input.setPlainText("写了一半的问题")
        self.panel.show_quiz(self.quiz())
        comp = self.panel.composer
        self.assertTrue(comp.input.isEnabled())
        self.assertEqual(comp.text(), "写了一半的问题")                  # 草稿不动
        self.assertTrue(comp.can_send())
        self.assertTrue(self.panel.dot.isVisibleTo(self.panel))
        comp.clear()
        self.assertEqual(comp.input.placeholderText(), PH_QUIZ)
        c = self.card()
        c._pick(1)
        c._button()
        self.assertFalse(self.panel.dot.isVisibleTo(self.panel))

    def test_ask_while_quiz_pending_sets_it_aside(self):
        self.panel.show_quiz(self.quiz())
        c = self.card()
        self.panel.composer.input.setPlainText("先问个别的")
        self.panel._send()
        self.assertIsNone(self.panel._card)                            # 题放一边
        self.assertFalse(self.panel.dot.isVisibleTo(self.panel))
        c._pick(1)                                                      # 留在对话里还能答
        c._button()
        self.assertEqual(self.done, [(0, True)])

    def test_skip(self):
        skipped = []
        self.panel.quiz_skipped.connect(skipped.append)
        self.panel.show_quiz(self.quiz())
        c = self.card()
        self.assertTrue(c.skip.isVisibleTo(c))
        c._pick(0)                                                      # 错一次以后也能跳
        c._button()
        self.assertTrue(c.skip.isVisibleTo(c))
        c.skip.click()
        self.assertEqual(c.state, "skipped")
        self.assertTrue(c.done)
        self.assertEqual(skipped, [0])
        self.assertEqual(self.done, [])                                 # 不算答完（不记对错）
        self.assertFalse(c.skip.isVisibleTo(c))
        self.assertFalse(c.button.isVisibleTo(c))
        self.assertIn("跳过", c.cap.text())
        self.assertIsNone(self.panel._card)
        self.assertFalse(self.panel.dot.isVisibleTo(self.panel))
        c._pick(1)                                                      # 跳过了就不能再选
        self.assertEqual(c.state, "skipped")

    def test_right_first_time(self):
        self.panel.show_quiz(self.quiz())
        c = self.card()
        self.assertEqual(c.state, "ask")
        self.assertFalse(c.button.isEnabled())                          # 没选不能确认
        c._pick(1)
        self.assertEqual(c.state, "pick")
        self.assertEqual(c.button.text(), "就选 B")
        c._pick(2)                                                      # 再点别的也行
        self.assertEqual(c.button.text(), "就选 C")
        c._pick(1)
        c._button()
        self.assertEqual(c.state, "right")
        self.assertEqual(self.done, [(0, True)])
        self.assertEqual(self.jumps, [])
        self.assertEqual(c.button.text(), "接着读下一节 ›")
        c._button()
        self.assertEqual(self.nexts, [1])
        self.assertTrue(c.button.isHidden())

    def test_wrong_then_right(self):
        self.panel.show_quiz(self.quiz())
        c = self.card()
        c._pick(0)
        c._button()
        self.assertEqual(c.state, "wrong")
        self.assertEqual(self.jumps, [(Loc(0, 6), "其野蛮程度不亚于")])
        self.assertEqual(self.done, [])                                  # 还有一次机会
        self.assertTrue(self.panel.composer.input.isEnabled())           # 有题也能问
        self.assertIn("回去看第一段", c.explain.text())
        self.assertNotIn("被看得和犯罪一样野蛮", c.explain.text())       # 不透露答案
        self.assertEqual(c.options[0].state, "no")
        c._pick(0)                                                       # 划掉的不能再选
        self.assertEqual(c.state, "wrong")
        c._pick(1)
        c._button()
        self.assertEqual(c.state, "right")
        self.assertEqual(self.done, [(0, True)])

    def test_second_wrong_reveals(self):
        self.panel.show_quiz(self.quiz())
        c = self.card()
        c._pick(0)
        c._button()
        c._pick(2)
        c._button()
        self.assertEqual(c.state, "reveal")
        self.assertEqual(self.done, [(0, False)])
        self.assertEqual(len(self.jumps), 2)
        self.assertIn("B. 被看得和犯罪一样野蛮", c.explain.text())
        self.assertEqual(c.options[1].state, "ok")
        self.assertTrue(self.panel.composer.input.isEnabled())

    def test_source_page_from_book(self):
        """跳到出处后书页报回来的页码写在卡上（EPUB 的页码只有书页知道）。"""
        self.panel.jump_to.connect(lambda loc, q: self.panel.set_context(loc, "第一章 · 第 7 页"))
        self.panel.show_quiz(self.quiz())
        c = self.card()
        c._pick(0)
        c._button()
        self.assertEqual(c.cap2.text(), "原文在第 7 页")
        self.assertFalse(c.cap2.isHidden())

    def test_no_focus_grab(self):
        win = QWidget()
        lay = QHBoxLayout(win)
        book = QLineEdit()
        lay.addWidget(self.panel)
        lay.addWidget(book)
        win.show()
        win.activateWindow()
        book.setFocus()
        pump(0.05)
        self.assertIs(QApplication.focusWidget(), book)
        self.panel.show_quiz(self.quiz())
        pump(0.02)
        self.assertIs(QApplication.focusWidget(), book)
        c = self.card()
        QTest.mouseClick(c.options[0], Qt.LeftButton)
        self.assertEqual(c.state, "pick")
        QTest.mouseClick(c.button, Qt.LeftButton)
        pump(0.02)
        self.assertEqual(c.state, "wrong")
        self.assertIs(QApplication.focusWidget(), book)                  # ←→ 还归书页
        win.close()

    def test_timeout_note_unlocks(self):
        self.panel.show_quiz(self.quiz())
        self.panel.quiz_timeout_note()
        c = self.card()
        self.assertFalse(c.note.isHidden())
        self.assertTrue(self.panel.composer.input.isEnabled())
        self.assertFalse(self.panel.dot.isVisibleTo(self.panel))
        c._pick(1)                                                       # 想答还能答
        c._button()
        self.assertEqual(self.done, [(0, True)])

    def test_section_title_on_card(self):
        self.panel.show_quiz(self.quiz(section=1))
        self.assertIn("第二章", self.card().title.text())

    def test_theme_switch_keeps_state(self):
        self.panel.show_quiz(self.quiz())
        c = self.card()
        c._pick(0)
        c._button()
        theme.set_theme("light", save=False)
        pump(0.02)
        self.assertEqual(c.state, "wrong")
        self.assertIn(theme.tok("t2", "light").lower(), c.explain.text().lower())
        theme.set_theme("dark", save=False)


if __name__ == "__main__":
    unittest.main()
