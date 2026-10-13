"""书页（EpubView / PdfView）：位置换算、挡板、高亮存取、出处定位。不开窗口（offscreen），不调 AI。

运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("READER_DATA_DIR", tempfile.mkdtemp())   # 偏好文件别写进真的数据目录
os.environ["READER_PDF_RENDER"] = "thread"                     # 测试里用线程出图（快、好收拾）

import pymupdf  # noqa: E402
from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from reader.books import Loc, open_book  # noqa: E402
from reader.ui import theme  # noqa: E402
from tests.test_books_ai import LONG, make_epub, make_pdf  # noqa: E402

app = QApplication.instance() or QApplication([])
theme.load_fonts()

from reader.ui.bookview.base import load_prefs  # noqa: E402
from reader.ui.bookview.epub_view import EpubView, cjk_quotes, glue_punct, range_rects  # noqa: E402
from reader.ui.bookview.pdf_view import PdfView, page_units, units_text  # noqa: E402

ZW = chr(0x200B)
QUOTE = "在人们看来，这种惩罚方式，其野蛮程度不亚于，甚至超过犯罪本身。"


def pump(cond=lambda: False, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def show(view, w=940, h=760):
    view.resize(w, h)
    view.show()
    pump(timeout=0.05)


def fake_click(pt: QPointF):
    return type("E", (), {"position": lambda _s: pt, "button": lambda _s: Qt.LeftButton,
                          "buttons": lambda _s: Qt.LeftButton})()


class _Spy:
    def __init__(self, sig):
        self.calls = []
        sig.connect(lambda *a: self.calls.append(a))


def reset_prefs():
    (theme.data_dir() / "bookview.json").unlink(missing_ok=True)


class EpubViewTests(unittest.TestCase):
    def setUp(self):
        reset_prefs()
        self.tmp = Path(tempfile.mkdtemp())
        paras = [LONG] * 4 + [QUOTE] + [LONG] * 4
        self.book = open_book(make_epub(self.tmp / "a.epub", [
            ("第一章 酷刑", paras), ("第二章 惩罚", [LONG] * 9), ("第三章 规训", [LONG] * 9)]))
        self.v = EpubView()
        show(self.v)
        self.v.open(self.book, None, [])
        pump(self.v.layout_done)

    def tearDown(self):
        self.v.close()
        self.v.deleteLater()

    def test_loc_position_roundtrip(self):
        v = self.v
        for d, blocks in enumerate(self.book.blocks):
            for b, blk in enumerate(blocks):
                loc = Loc(d, b)
                dd, pos = v._pos_of(loc)
                self.assertEqual((dd, v._loc_of(dd, pos)), (d, loc))
                # 块内偏移 = 字符偏移：文档里这一段的字跟书里的块一字不差
                doc = v._ensure_paged(d).doc
                self.assertEqual(doc.findBlockByNumber(b).text().replace(ZW, ""), blk.text)
        # goto 之后，current_loc 的页码跟底栏一致；再 goto(current_loc()) 回到同一页
        for loc in (Loc(0, 5), Loc(1, 3), Loc(2, 8)):
            v.goto(loc)
            page = v._cur
            here = v.current_loc()
            self.assertEqual(v.page_label(here), f"第 {int(v.bar._page_args[0])} 页")
            v.goto(Loc(0, 0))
            v.goto(here)
            self.assertEqual(v._cur, page)
        # 超出范围的 Loc 落到书尾，不报错
        v.goto(Loc(99, 0))
        self.assertEqual(v._cur[0], 2)

    def test_pages_count_and_turn(self):
        v = self.v
        total, exact = v._total_pages()
        self.assertTrue(exact)
        self.assertGreater(total, 6)
        seen = []
        act = _Spy(v.activity)
        v.goto(Loc(0, 0))
        for _ in range(total + 3):
            seen.append(v.bar._page_args[0])
            v._user_turn(1)
        self.assertEqual(seen[:total], list(range(1, total + 1)))   # 一页一页，不跳不重
        self.assertEqual(seen[-1], total)                              # 到书尾停住
        self.assertGreater(len(act.calls), total)
        # 每章从新的一页开始
        for d in (1, 2):
            v.goto(Loc(d, 0))
            self.assertEqual(v._cur, (d, 0))

    def test_barrier_paged(self):
        v = self.v
        v.BLOCK = True                  # 挡板模式（2026-09-25 起默认不挡，见 test_barrier_soft）
        hit = _Spy(v.barrier_hit)
        sec0 = self.book.sections[0]
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(60):
            v.page_forward()
        self.assertTrue(hit.calls)
        self.assertEqual(hit.calls[0], (0,))
        self.assertLess(v.current_loc(), sec0.end)
        self.assertTrue(v.hint.isVisible())
        self.assertIn("先答完", v.hint.text.text())
        # 往回翻不挡，提示走了
        before = v._cur
        v.page_back()
        self.assertNotEqual(v._cur, before)
        self.assertFalse(v.hint.isVisible())
        # 目录也不能跳过去
        n = len(hit.calls)
        v._toc_picked(self.book.sections[1].start)
        self.assertLess(v.current_loc(), sec0.end)
        self.assertEqual(len(hit.calls), n + 1)
        # 拿掉挡板就能往后
        v.set_barrier(None)
        v.goto(self.book.sections[-1].start)
        self.assertGreaterEqual(v.current_loc(), self.book.sections[-1].start)

    def test_barrier_scroll_mode(self):
        v = self.v
        v.BLOCK = True
        v.set_mode("scroll")
        self.addCleanup(v.set_mode, "paged")
        hit = _Spy(v.barrier_hit)
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(80):
            v._scroll_by(300)
        self.assertTrue(hit.calls)
        end = self.book.sections[0].end
        # 屏幕最下面也没越过挡板
        bd, bpos = v._pos_of(end)
        by = v._line_top(v._ensure_scroll(bd).sdoc, bpos)
        bot = v._norm_forward(v._sd, v._soff + v._scroll_vis())
        self.assertLessEqual(bot, (bd, by + 0.5))
        v._scroll_by(-500)
        self.assertFalse(v.hint.isVisible())
        v.set_mode("paged")

    def test_highlight_roundtrip(self):
        v = self.v
        changed = _Spy(v.highlights_changed)
        doc = v._ensure_paged(0).doc
        b2, b3 = doc.findBlockByNumber(2), doc.findBlockByNumber(3)
        a, b = b2.position() + 10, b3.position() + 7          # 跨两段
        new = v.add_highlight(0, a, b)
        self.assertEqual(len(new), 2)
        self.assertEqual(len(changed.calls), 1)
        hs = v.highlights()
        self.assertEqual([(h["doc"], h["block"], h["start"], h["end"]) for h in hs],
                         [(0, 2, 10, len(LONG)), (0, 3, 0, 7)])
        self.assertEqual(hs[0]["text"], LONG[10:])
        self.assertEqual(hs[0]["group"], hs[1]["group"])
        self.assertEqual(v.bar.hl.text(), "高亮 1")
        # 存起来再打开，位置不变
        v2 = EpubView()
        show(v2)
        v2.open(self.book, Loc(0, 2), hs)
        self.assertEqual([(x, y) for x, y, _ in v2._hl_ranges(0)], [(a, b2.position() + b2.length() - 1),
                                                                     (b3.position(), b)])
        # 重叠的并起来
        v2.add_highlight(0, b2.position() + 5, b2.position() + 20)
        blk2 = [h for h in v2.highlights() if h["block"] == 2]
        self.assertEqual([(h["start"], h["end"]) for h in blk2], [(5, len(LONG))])
        # 点在高亮上 → 取消（整组一起）
        h = v2._hls[0]
        v2.remove_highlight(h)
        self.assertTrue(all(x.get("group") != h.get("group") for x in v2.highlights()))
        v2.close()

    def test_mark_source(self):
        v = self.v
        v.goto(Loc(2, 5))
        before = v.bar._page_args[0]
        v.mark_source(Loc(0, 5), QUOTE)       # 第 0 块是章标题，引文是第 5 块
        d, a, b = v._src
        doc = v._ensure_paged(0).doc
        self.assertEqual(d, 0)
        self.assertEqual(doc.findBlock(a).blockNumber(), 5)
        self.assertEqual(v._text(0, a, b), QUOTE[:-1])   # 句末标点不算
        self.assertTrue(v.pill.isVisible())
        self.assertIn(f"回到刚才的第 {before} 页", v.pill.btn.text())
        self.assertEqual(v.page_label(v.current_loc()), v.page_label(Loc(0, 5)))
        # 回去
        v._go_back()
        self.assertEqual(v.bar._page_args[0], before)
        self.assertFalse(v.pill.isVisible())
        self.assertIsNotNone(v._src)              # 标记留着，答完题由读书界面清掉
        v.clear_source()
        self.assertIsNone(v._src)
        # 标点、空白不一样；Loc 差了几块；AI 改了一个字 —— 都能找到
        for q, loc in (("其野蛮程度不亚于 甚至超过犯罪本身", Loc(0, 5)),
                       ("这种惩罚方式其野蛮程度不亚于甚至超过犯罪本身", Loc(0, 1)),
                       ("这种惩罚方式，其野蛮程度不低于，甚至超过犯罪本身", Loc(0, 5))):
            v.mark_source(loc, q)
            self.assertIsNotNone(v._src, q)
            self.assertEqual(doc.findBlock(v._src[1]).blockNumber(), 5, q)
        # 找不到：至少跳到那一块，不标
        v.mark_source(Loc(1, 3), "完全不相干的一句话书里根本没有")
        self.assertIsNone(v._src)
        self.assertEqual(v.current_loc().doc, 1)

    def test_selection_popover_and_ask(self):
        v = self.v
        v.goto(Loc(0, 0))
        asked = _Spy(v.ask_ai)
        v.demo_select()
        self.assertTrue(v.popover.isVisible())
        self.assertEqual(v.popover.b_hl.text(), "高亮")
        text = v._text(*v._sel)
        v._pop_ask()
        self.assertEqual(asked.calls[0][0], text)
        self.assertIsInstance(asked.calls[0][1], Loc)
        self.assertIsNotNone(v._asked)
        self.assertFalse(v.popover.isVisible())
        # 选中再高亮，然后单击那处高亮 → 小条变「取消高亮」
        v.demo_select()
        v._pop_highlight()
        d = v._cur[0]
        a, b, _ = v._hl_ranges(d)[0]
        r = v._to_screen(d, range_rects(v._doc(d), a, b)[0][0])
        ev = fake_click(QPointF(r.center()))
        v._press(ev)
        v._release(ev)
        self.assertTrue(v.popover.isVisible())
        self.assertEqual(v.popover.b_hl.text(), "取消高亮")
        v._pop_unhighlight()
        self.assertEqual(v.highlights(), [])

    def test_quote_breaks_keep_offsets(self):
        """引号旁边补的换行点（零宽空格）不影响高亮存的偏移、也不进选出来的字。"""
        para = "这一段讲自我审判和自我谴责”（Example，第1部分）。它不是“明确证据”（evidence），也不是。" * 3
        book = open_book(make_epub(self.tmp / "q.epub", [("第一章", [LONG, para, LONG])]))
        v = EpubView()
        show(v)
        v.open(book, None, [])
        doc = v._ensure_paged(0).doc
        blk = doc.findBlockByNumber(2)
        self.assertIn(ZW, blk.text())
        self.assertEqual(blk.text().replace(ZW, ""), para)
        k = para.index("明确证据")
        dk = blk.text().index("明确证据")
        new = v.add_highlight(0, blk.position() + dk, blk.position() + dk + 4)
        self.assertEqual((new[0]["start"], new[0]["end"], new[0]["text"]), (k, k + 4, "明确证据"))
        v2 = EpubView()
        show(v2)
        v2.open(book, Loc(0, 2), v.highlights())
        a, b, _ = v2._hl_ranges(0)[0]
        self.assertEqual(v2._text(0, a, b), "明确证据")
        # 跨过插入点的高亮
        k2 = para.index("谴责”（Exa")
        d2 = blk.text().index("谴责")
        d3 = blk.text().index("Example") + 3
        h = v.add_highlight(0, blk.position() + d2, blk.position() + d3)[0]
        self.assertEqual(h["text"], "谴责”（Exa")
        self.assertEqual(h["start"], k2)
        self.assertEqual(v._text(0, blk.position() + d2, blk.position() + d3), "谴责”（Exa")
        v.close()
        v2.close()

    def test_barrier_soft(self):
        """默认不挡：一页页读过节末发一次 barrier_hit，照样往后翻；目录随便跳，跳过去不发。"""
        v = self.v
        self.assertFalse(EpubView.BLOCK)
        hit = _Spy(v.barrier_hit)
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(60):
            v.page_forward()
        self.assertGreaterEqual(v.current_loc(), self.book.sections[1].start)   # 过去了
        self.assertEqual(hit.calls, [(0,)])                                      # 只发一次
        self.assertFalse(v.hint.isVisible())
        # 滚动也一样
        v.set_mode("scroll")
        self.addCleanup(v.set_mode, "paged")
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(80):
            v._scroll_by(300)
        self.assertEqual(hit.calls, [(0,), (0,)])
        self.assertGreaterEqual(v.current_loc(), self.book.sections[1].start)
        v.set_mode("paged")
        # 目录跳过去：不拦、不发
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        v._open_toc()
        self.assertTrue(all(v.toc_panel.list.item(i).flags() & Qt.ItemIsEnabled
                            for i in range(v.toc_panel.list.count())))
        v.toc_panel.close_panel()
        v._toc_picked(self.book.sections[2].start)
        self.assertEqual(v.current_loc(), self.book.sections[2].start)
        self.assertEqual(len(hit.calls), 2)
        # 双页：右页照画
        show(v, 1580, 1000)
        v.set_display("spread")
        self.addCleanup(v.set_display, "paged")
        pump(v.layout_done)
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(30):
            if v._shown_pages() == [0, 1] and v._page_span(*v._cur)[1] > v._barrier_pos()[1]:
                break
            v.page_forward()
        self.assertEqual(v._shown_pages(), [0, 1])

    def test_font_size_keeps_position(self):
        v = self.v
        loc_spy = _Spy(v.location_changed)
        v.goto(Loc(1, 4))
        loc = v.current_loc()
        page18 = v.bar._page_args[0]
        pages18 = v._total_pages()[0]
        v.set_font_px(22)
        pump(v.layout_done)
        self.assertGreater(v._total_pages()[0], pages18)
        # 位置一段都不挪；来回换几次也不往前漂（原来每换一次退一两段，退过节首顶条的节名就错了）
        self.assertEqual(v.current_loc(), loc)
        for px in (15, 24, 16, 20, 18):
            v.set_font_px(px)
            self.assertEqual(v.current_loc(), loc, px)
            pump(v.layout_done)
            self.assertEqual(v.current_loc(), loc, px)
            # 底栏、报出去的页码、current_loc 的页码三处一致
            self.assertEqual(v.page_label(loc), f"第 {v.bar._page_args[0]} 页")
            self.assertEqual(loc_spy.calls[-1][0], loc)
        self.assertEqual(v.bar._page_args[0], page18)       # 回到 18px：还是原来那页

    def test_style_changes_keep_position_and_persist(self):
        """Aa 里换字体、粗细、行距、每行字数、特大字号：位置不挪、记住选择；恢复默认回到 18px 黑体。"""
        v = self.v
        v.goto(Loc(1, 4))
        loc = v.current_loc()
        faces = theme.book_faces()
        for face in faces:
            v.set_face(face)
            self.assertEqual(v.current_loc(), loc, face)
        v.set_face(faces[-1])
        v.set_weight(2)
        v.set_line("loose")
        v.set_width("narrow")
        self.assertEqual(v.current_loc(), loc)
        self.assertLessEqual(v._col_w, 28 * v.font_px())
        self.assertEqual(v._col_w % v.font_px(), 0)         # 栏宽是字号的整数倍
        v.set_font_px(64)                                     # 不再卡在 24
        self.assertEqual(v.font_px(), 64)
        pump(v.layout_done)
        self.assertEqual(v.current_loc(), loc)
        prefs = load_prefs()
        self.assertEqual((prefs["epub_px"], prefs["epub_face"], prefs["epub_weight"], prefs["epub_line"],
                          prefs["epub_width"]), (64, faces[-1], 2, "loose", "narrow"))
        w2 = EpubView()                                       # 下次打开还是这样
        self.assertEqual((w2.font_px(), w2.style().face, w2.line_key(), w2.width_key()),
                         (64, faces[-1], "loose", "narrow"))
        w2.deleteLater()
        v.reset_style()
        self.assertEqual((v.font_px(), v.style().face, v.style().weight, v.line_key(), v.width_key()),
                         (18, "sans", 1, "normal", "normal"))
        self.assertEqual(v.current_loc(), loc)

    def test_zoom_steps(self):
        v = self.v
        v.zoom(1)
        self.assertEqual(v.font_px(), 20)
        v.zoom(-1)
        v.zoom(-1)
        self.assertEqual(v.font_px(), 17)
        v.set_font_px(72)
        v.zoom(1)
        self.assertEqual(v.font_px(), 72)                     # 到头了就停在那儿

    def test_chapter_left_in_bar(self):
        v = self.v
        v.goto(Loc(0, 0))
        info = v.bar.info.toolTip() or v.bar.info.text()
        self.assertIn("本章还剩", info)
        n = v._chapter_left(v.current_loc(), v._global_page(*v._cur))
        pages = len(v._ensure_paged(0).pages)
        self.assertEqual(n, pages - 1)
        v._show_page(0, pages - 1)
        self.assertIn("本章最后一页", v.bar.info.toolTip() or v.bar.info.text())

    def test_font_size_estimates_and_reports_page(self):
        """换字号后没排完时：页数按重排前的页数估（差不多准），前面的章排好就再报一次位置（页码准了）。"""
        paras = [LONG] * 16
        book = open_book(make_epub(self.tmp / "many.epub",
                                   [(f"第{i}章", paras if i % 3 else paras[:5]) for i in range(1, 25)]))
        v = EpubView()
        show(v)
        v.open(book, Loc(20, 3), [])
        pump(v.layout_done)
        loc = v.current_loc()
        spy = _Spy(v.location_changed)
        v.set_font_px(24)
        est, exact = v._total_pages()
        self.assertFalse(exact)
        self.assertEqual(spy.calls[-1][0], loc)            # 段没变也马上报一次（页码变了）
        pump(lambda: all(v._chs[d].doc is not None for d in v._nonempty if d < 20))
        pump(timeout=0.05)
        real_page = v.bar._page_args[0]
        pump(v.layout_done)
        tot = v._total_pages()[0]
        self.assertLess(abs(est - tot) / tot, 0.15, (est, tot))
        self.assertEqual(real_page, v.bar._page_args[0])  # 前面排好时页码就已经是准的
        self.assertEqual(v.page_label(spy.calls[-1][0]), f"第 {real_page} 页")
        v.set_font_px(18)
        v.close()

    def test_spread_two_pages(self):
        v = self.v
        show(v, 1580, 1000)
        pump(v.layout_done)
        v.goto(Loc(1, 4))
        loc = v.current_loc()
        v.set_display("spread")
        self.addCleanup(v.set_display, "paged")
        pump(v.layout_done)
        self.assertTrue(v.two_pages())
        self.assertEqual(v.current_loc(), loc)                 # 换成双页不挪位置
        d, p = v._cur
        self.assertEqual(p % 2, 0)                             # 左页总是一章里的第 0、2、4… 页
        cur, _tot, _approx, last = v.bar._page_args
        self.assertEqual(last, cur + 1)
        self.assertIn(f"{cur}–{last}", v.bar.page.text())
        # 翻页一次两页；每章从左页开始；往回翻回到原来那一对
        seen = []
        v.goto(Loc(0, 0))
        for _ in range(40):
            seen.append(v._cur)
            v.page_forward()
        for d2 in (1, 2):
            self.assertIn((d2, 0), seen)
        self.assertTrue(all(pg % 2 == 0 for _d, pg in seen))
        v.goto(loc)
        before = v._cur
        v.page_forward()
        v.page_back()
        self.assertEqual(v._cur, before)
        # 点右页选字，小条出在右页上
        pt = QPointF(v._col_x + v._col_w + 72 + v._col_w / 2, 300)
        v._double(fake_click(pt))
        self.assertIsNotNone(v._sel)
        right_page = v._cur[1] + 1
        self.assertEqual(v._page_of(v._sel[0], v._sel[1]), right_page)
        self.assertTrue(v.popover.isVisible())
        self.assertGreater(v.popover.x(), v._col_x + v._col_w)
        # 换字号还是双页、不挪位置（翻过页，「读到这儿」是这两页上第一个开头的段）
        loc = v.current_loc()
        v.set_font_px(22)
        pump(v.layout_done)
        self.assertTrue(v.two_pages())
        self.assertEqual(v.current_loc(), loc)
        v.set_font_px(18)
        # 窗口窄了先单页，宽回来又是双页，位置都不挪
        show(v, 720, 760)
        v._relayout()
        self.assertFalse(v.two_pages())
        self.assertEqual(v.display(), "spread")
        self.assertEqual(v.current_loc(), loc)
        show(v, 1580, 1000)
        v._relayout()
        self.assertTrue(v.two_pages())
        self.assertEqual(v.current_loc(), loc)
        # 记住了
        v2 = EpubView()
        self.assertEqual(v2.display(), "spread")
        v2.close()

    def test_spread_barrier(self):
        """（挡板模式）双页时右页过了挡板就不画、点不到；往后翻被挡住。"""
        v = self.v
        v.BLOCK = True
        show(v, 1580, 1000)
        v.set_display("spread")
        self.addCleanup(v.set_display, "paged")
        pump(v.layout_done)
        hit = _Spy(v.barrier_hit)
        v.set_barrier(0)
        v.goto(Loc(0, 0))
        for _ in range(30):
            v.page_forward()
        self.assertTrue(hit.calls)
        d, p = v._cur
        self.assertEqual(d, 0)
        pages = v._ensure_paged(d).pages
        end = v._barrier_pos()
        for k in v._shown_pages():
            self.assertLess((d, pages[p + k]), end)
        self.assertLess(v.current_loc(), self.book.sections[0].end)   # 没画出来的右页不算「读到这儿」

    def test_keys_turn_pages_and_count_as_activity(self):
        from PySide6.QtTest import QTest
        v = self.v
        v.goto(Loc(0, 0))
        act = _Spy(v.activity)
        QTest.keyClick(v, Qt.Key_Right)
        QTest.keyClick(v, Qt.Key_Space)
        self.assertEqual(v._cur, (0, 2))
        QTest.keyClick(v, Qt.Key_Left)
        self.assertEqual(v._cur, (0, 1))
        self.assertGreaterEqual(len(act.calls), 3)

    def test_dim(self):
        self.v.set_dimmed(True)
        self.assertTrue(self.v.dim.isVisible())
        self.v.set_dimmed(False)
        self.assertFalse(self.v.dim.isVisible())


def make_cjk_pdf(path: Path, pages: int = 12) -> Path:
    doc = pymupdf.open()
    body = ("惩罚的方式在不到一百年的时间里发生了变化。" * 3 + QUOTE + "公开的酷刑消失了，监禁取而代之。" * 4)
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_textbox(pymupdf.Rect(72, 72, 380, 700), f"第{i + 1}页。" + body, fontname="china-s", fontsize=14)
    doc.save(path)
    return path


class PdfViewTests(unittest.TestCase):
    def setUp(self):
        reset_prefs()
        self.tmp = Path(tempfile.mkdtemp())
        self.book = open_book(make_pdf(self.tmp / "a.pdf", 25))
        self.v = PdfView()
        show(self.v)
        self.v.open(self.book, None, [])

    def tearDown(self):
        self.v._renderer.close()
        self.v.close()
        self.v.deleteLater()

    def test_render_and_goto(self):
        v = self.v
        self.assertTrue(pump(lambda: v.pending() == 0, 10))
        self.assertGreater(v._stats["renders"], 0)
        v.goto(Loc(7))
        self.assertEqual(v.current_loc().doc in (6, 7), True)
        self.assertEqual(v.page_label(Loc(7)), "第 8 页")
        self.assertTrue(pump(lambda: v.pending() == 0, 10))
        # 图按设备像素出：宽度 = 显示宽度 × dpr
        key = next(k for k in v._cache if k[0] == 7)
        self.assertEqual(key[1], round(v._ws[7] * v._dpr()))

    def test_spread_two_pages(self):
        v = self.v
        show(v, 1580, 1000)
        v.set_fit("page")
        v.set_spread(True)
        self.assertTrue(v.two_pages())
        self.assertEqual(v._tops[0], v._tops[1])                 # 第 1、2 页一排
        self.assertLess(v._tops[1], v._tops[2])
        self.assertLess(v._xs[0] + v._ws[0], v._xs[1])           # 左右并排，中间有缝
        v.goto(Loc(7))                                           # 第 8 页在第 7、8 页那一排
        self.assertEqual(v.current_loc().doc, 6)
        self.assertEqual(v.bar._page_args[0], 7)
        self.assertEqual(v.bar._page_args[3], 8)
        self.assertIn("7–8", v.bar.page.text())
        self.assertIn("双页", v.bar.aa.text())
        self.assertTrue({6, 7} <= set(v._visible()))
        v.next_page()                                            # › 翻一排
        self.assertEqual(v.current_loc().doc, 8)
        # 点右页算右页
        y = v._tops[8] - v._y + v._hs[8] / 2
        self.assertEqual(v._pdf_point(QPointF(v._xs[9] + 20, y))[0], 9)
        self.assertEqual(v._pdf_point(QPointF(v._xs[8] + 20, y))[0], 8)
        # （挡板模式）挡板在这一排中间：右页不画、点不到
        v.BLOCK = True
        v._barrier_loc = lambda: Loc(9)
        self.assertNotIn(9, v._visible())
        self.assertEqual(v._pdf_point(QPointF(v._xs[9] + 20, y))[0], 8)
        del v._barrier_loc
        v.BLOCK = False
        # 窗口窄了先单页，位置不丢
        show(v, 720, 760)
        v._relayout()
        self.assertFalse(v.two_pages())
        self.assertTrue(v.spread())
        self.assertIn(v.current_loc().doc, (8, 9))
        v2 = PdfView()
        self.assertTrue(v2.spread())                            # 记住了
        v2.close()

    def test_barrier_soft(self):
        """默认不挡：滚过第 1 节末尾发一次 barrier_hit，照样往下读。"""
        v = self.v
        hit = _Spy(v.barrier_hit)
        v.set_barrier(0)
        for _ in range(40):
            v.page_forward()
        self.assertEqual(hit.calls, [(0,)])
        self.assertGreater(max(v._visible()), 9)
        self.assertFalse(v.hint.isVisible())
        self.assertTrue(v._toc_allowed(Loc(20)))

    def test_barrier(self):
        v = self.v
        v.BLOCK = True
        hit = _Spy(v.barrier_hit)
        v.set_barrier(0)          # 第 1 节 = 第 1–10 页
        for _ in range(40):
            v.page_forward()
        self.assertTrue(hit.calls)
        self.assertEqual(hit.calls[0], (0,))
        vis = v._visible()
        self.assertLessEqual(max(vis), 9)        # 第 11 页（下标 10）一点都看不到
        self.assertTrue(v.hint.isVisible())
        v.page_back()
        self.assertFalse(v.hint.isVisible())
        v.set_barrier(None)
        v.goto(Loc(20))
        self.assertGreaterEqual(v.current_loc().doc, 19)

    def test_highlight_roundtrip(self):
        v = self.v
        v.goto(Loc(3))
        units = v._page_units(3)
        self.assertTrue(units)
        v._sel = (3, 2, 6)
        v._finish_selection()
        self.assertTrue(v.popover.isVisible())
        v._pop_highlight()
        hs = v.highlights()
        self.assertEqual(len(hs), 1)
        self.assertEqual(hs[0]["page"], 3)
        self.assertEqual(hs[0]["text"], units_text(units[2:7]))
        x0, y0, x1, y1 = hs[0]["rects"][0]
        self.assertAlmostEqual(x0, units[2].x0, places=1)
        v2 = PdfView()
        show(v2)
        v2.open(self.book, Loc(3), hs)
        self.assertEqual(v2.highlights(), hs)
        self.assertEqual(v2.bar.hl.text(), "高亮 1")
        v2.remove_highlight(v2._hls[0])
        self.assertEqual(v2.highlights(), [])
        v2._renderer.close()
        v2.close()

    def test_mark_source_text_layer(self):
        v = self.v
        v.goto(Loc(15))
        v.mark_source(Loc(4), "quiet discipline")
        self.assertEqual(v._src[0], 4)
        self.assertTrue(v.pill.isVisible())
        self.assertIn("第 5 页", v.pill.text.text())
        self.assertIn("回到刚才的第 16 页", v.pill.btn.text())
        # 页码给错了一页：前后页也找
        v.mark_source(Loc(8), "Page 10.")
        self.assertEqual(v._src[0], 9)
        # 找不到：跳到那一页
        v.mark_source(Loc(12), "nothing like this anywhere")
        self.assertIsNone(v._src)
        self.assertIn(v.current_loc().doc, (12, 13))

    def test_cjk_units_and_cross_line_quote(self):
        book = open_book(make_cjk_pdf(self.tmp / "c.pdf"))
        v = PdfView()
        show(v)
        v.open(book, Loc(0), [])
        units = v._page_units(2)
        self.assertTrue(all(len(u.text) == 1 for u in units if u.cjk))   # 汉字一个一个
        self.assertEqual(units_text(units[:4]), "第3页。")
        v.mark_source(Loc(2), QUOTE)     # 这句在 PDF 里跨行：search_for 找不到，按字比对
        self.assertIsNotNone(v._src)
        self.assertEqual(v._src[0], 2)
        self.assertGreaterEqual(len(v._src[1]), 2)
        v._renderer.close()
        v.close()

    def test_scanned_no_selection(self):
        doc = pymupdf.open()
        for _ in range(3):
            pg = doc.new_page()
            pg.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        doc.save(self.tmp / "s.pdf")
        book = open_book(self.tmp / "s.pdf")
        book.ocr = None            # 当作这台电脑识别不了
        self.assertEqual(book.text_source, "none")
        v = PdfView()
        show(v)
        v.open(book, Loc(0), [])
        self.assertTrue(v.bar.note.isVisibleTo(v))
        self.assertIn("扫描版", v.bar.note.text())
        self.assertFalse(v.bar.hl.isVisibleTo(v))
        ev = fake_click(QPointF(v.canvas.width() / 2, 200))
        v._press(ev)
        v._release(ev)
        self.assertIsNone(v._sel)
        self.assertFalse(v.popover.isVisible())
        # 识别过的扫描版：用 page_words 选字、找出处（中文一个字一个词，没有空格）
        words = [(72 + 14 * k, 80, 86 + 14 * k, 96, c, 0) for k, c in enumerate("公开的酷刑消失了")]
        book.page_words = lambda p: words if p == 1 else []
        v.mark_source(Loc(1), "酷刑消失")
        self.assertEqual(v._src[0], 1)
        self.assertAlmostEqual(v._src[1][0][0], 72 + 14 * 3)
        v._renderer.close()
        v.close()

    def test_page_units_split(self):
        us = page_units([(0, 0, 40, 10, "测试之书", 3), (50, 0, 80, 10, "Author", 3)])
        self.assertEqual([u.text for u in us], ["测", "试", "之", "书", "Author"])
        self.assertAlmostEqual(us[1].x0, 10)            # 40 宽、4 个字，每字 10
        self.assertEqual(units_text(us), "测试之书Author")


class PdfProcessRendererTest(unittest.TestCase):
    def test_subprocess_renders(self):
        """默认的子进程出图能起来、能出图。"""
        os.environ.pop("READER_PDF_RENDER", None)
        try:
            tmp = Path(tempfile.mkdtemp())
            book = open_book(make_pdf(tmp / "p.pdf", 5))
            v = PdfView()
            show(v)
            v.open(book, None, [])
            self.assertEqual(v._renderer.mode, "process")
            self.assertTrue(pump(lambda: v.pending() == 0, 20))
            v._renderer.close()
            v.close()
        finally:
            os.environ["READER_PDF_RENDER"] = "thread"


if __name__ == "__main__":
    unittest.main()


class TypesetTests(unittest.TestCase):
    def test_glue_punct(self):
        # 转制留下的空格：注释编号前、中文标点前换成粘住的空格（字数不变）
        t = "传统战争 [1] ，那么（ 注 ）"
        g = glue_punct(t)
        self.assertEqual(len(g), len(t))
        self.assertNotIn(" ", g)
        self.assertEqual(glue_punct("Hello world, 中文"), "Hello world, 中文")

    def test_cjk_quotes(self):
        t = "从“社会主义”或“Ok”和“word”"
        hits = cjk_quotes(t)
        self.assertEqual([t[i] for i in hits], ["“", "”"])     # 引英文的（“Ok”、“word”）不动
        self.assertEqual(hits, [1, 6])
