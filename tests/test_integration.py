"""四个部分接在一起以后的测试（整合那一轮加的）：读书界面 + 真的左栏（AiPanel）+ 真的书页（EpubView）。

- 「接着读下一节 ›」→ 书页翻到下一节开头
- 答错跳到出处后，左栏「看得到」和卡上「原文在第 N 页」用的是出处那页；EPUB 排完再报一次位置时跟着刷新
- 开书时焦点给书页（不落在输入框上）；书在后台打开时写「正在打开……」
- 顶上几条（出处小条 /「刚才离开了……」）叠着放，不重叠
- 读完马上发 finished，笔记由读书笔记页在后台写；紧急出口不写；书打不开回书架
- 扫描版：书页和后台识别各拿一个 OcrStore，done_pages 会重读；「还在识别」只在真的在识别时说
- 开发开关 READER_NO_LOCK=1

不调真 AI、不进锁屏（QT_QPA_PLATFORM=offscreen）。跑法：python -m unittest discover -s tests
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

import pymupdf  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from reader.ai.companion import Companion, Quiz  # noqa: E402
from reader.books import Loc, ocr  # noqa: E402
from tests.test_lock import Clock  # noqa: E402
from tests.test_reading_ui import BgQueue, FakeAI, FakeWatcher, ensure_epub  # noqa: E402

app = QApplication.instance() or QApplication([])

from reader.library import Library, OcrQueue  # noqa: E402
from reader.ui import theme  # noqa: E402

theme.load_fonts()

from reader.ui.ai_panel import AiPanel  # noqa: E402
from reader.ui.bookview.epub_view import EpubView  # noqa: E402
from reader.ui.reading import ReadingDeps, ReadingScreen, no_lock, page_number  # noqa: E402
from reader.ui.stand_ins import MemoryLibrary  # noqa: E402


def pump(until=None, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.005)
    if until is not None:
        raise AssertionError("等不到")


def _no(*_a, **_k):
    raise AssertionError("开发模式 / 这个测试里不该调到这里（不能锁屏）")


class Rig:
    """真的左栏 + 真的 EPUB 书页 + 假 AI + 假时钟；不锁屏（locked=False）。"""

    def __init__(self, tmp: Path, *, minutes=30, logged_in=True, open_book=None, locked=False):
        self.clock = Clock(1_700_000_000.0)
        self.lib = MemoryLibrary(notes_dir=tmp / "notes")
        self.entry = self.lib.add(ensure_epub(tmp))
        self.ai = FakeAI()
        self.bg = BgQueue()
        self.watcher = FakeWatcher()
        kw = {"open_book": open_book} if open_book else {}
        deps = ReadingDeps(
            make_view=lambda book: EpubView(),
            make_panel=AiPanel,
            make_companion=lambda book: Companion(book, runner=self.ai.run, ask_once=self.ai.ask_once),
            logged_in=lambda: logged_in, run_bg=self.bg,
            make_window=_no, make_overlays=_no, make_watcher=lambda: self.watcher,
            release_top=lambda w: None, clock=self.clock,
            state_path=tmp / "session.json", status_path=tmp / "status.json", **kw)
        self.rs = ReadingScreen(self.entry, minutes, self.lib, deps=deps, locked=locked)
        self.summaries: list[dict] = []
        self.rs.finished.connect(self.summaries.append)

    def start(self, open_now: bool = True):
        self.rs.start()
        if open_now:
            self.open()
        return self

    def open(self):
        for _ in range(2):             # 打开书、查登录
            fn, cb = self.bg.jobs.pop(0)
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                r = e
            cb(r)
        pump(timeout=0.05)

    @property
    def view(self) -> EpubView:
        return self.rs.view

    @property
    def panel(self) -> AiPanel:
        return self.rs.panel

    def to_quiz(self) -> None:
        """出好第 0 节的题，往后翻到挡板：左栏出题。"""
        self.bg.flush()
        for _ in range(60):
            if self.panel._card is not None:
                return
            self.view.page_forward()
        raise AssertionError("没翻到挡板")

    def close(self):
        if not self.rs.ended:
            self.rs.dispose()
        self.rs.widget.deleteLater()


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in ("READER_DATA_DIR", "READER_NO_LOCK")}
        os.environ["READER_DATA_DIR"] = str(self.tmp / "data")
        os.environ.pop("READER_NO_LOCK", None)
        theme.set_theme("dark", save=False)
        self.rigs: list[Rig] = []

    def rig(self, **kw) -> Rig:
        r = Rig(self.tmp, **kw)
        self.rigs.append(r)
        return r

    def tearDown(self):
        for r in self.rigs:
            r.close()
        pump(timeout=0.02)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


# ── 1 · 接着读下一节 ────────────────────────────────────────
class NextSectionTests(Case):
    def test_continue_button_goes_to_next_section(self):
        r = self.rig().start()
        r.to_quiz()
        card = r.panel._card
        card._pick(1)                                      # 对了
        card._button()
        self.assertTrue(card.done)
        self.assertEqual(r.rs.last_quiz_section, 0)
        card._button()                                     # 「接着读下一节 ›」
        book = r.rs.book
        self.assertEqual(book.section_at(r.view.current_loc()).idx, 1)
        self.assertEqual(r.view.current_loc(), book.sections[1].start)
        self.assertIs(r.rs.widget.focusWidget(), r.view)   # ←→ 马上能接着翻

    def test_label_shows_next_section_when_it_starts_mid_page(self):
        # 下一节从页中间开始：书页报的位置（屏幕最上面那段）还在上一节，顶条要写下一节
        r = self.rig().start()
        book = r.rs.book
        top_of_page = book.sections[0].start           # 假装跳过去后屏幕最上面还是第 0 节
        r.rs._pin = [1, None]
        r.rs._on_location(top_of_page)
        self.assertEqual(r.rs.strip.crumb.section, book.sections[1].title)
        r.rs._on_location(book.sections[1].start)      # 一翻页：回到按位置算
        self.assertIsNone(r.rs._pin)
        r.rs._on_location(top_of_page)
        self.assertEqual(r.rs.strip.crumb.section, book.sections[0].title)


# ── 1b · 小测不挡人、能跳过（2026-09-25 起） ─────────────────────
class NoBlockTests(Case):
    def test_quiz_shows_but_reading_goes_on(self):
        r = self.rig().start()
        r.to_quiz()                                        # 翻过第 0 节末尾：出题
        card = r.panel._card
        self.assertEqual(card.quiz.section, 0)
        for _ in range(20):                                # 不答，接着往后翻
            r.view.page_forward()
        book = r.rs.book
        self.assertGreaterEqual(r.view.current_loc(), book.sections[1].start)
        self.assertFalse(r.view.hint.isVisible())
        self.assertTrue(r.panel.composer.input.isEnabled())
        self.assertEqual(r.rs.gate, 2)                     # 记号跟着主人挪到正在读的节

    def test_toc_jump_skips_without_quiz(self):
        r = self.rig().start()
        r.bg.flush()
        book = r.rs.book
        r.view._toc_picked(book.sections[2].start)         # 跳过前两节
        self.assertEqual(book.section_at(r.view.current_loc()).idx, 2)
        self.assertIsNone(r.panel._card)                   # 跳过去的节不出题
        self.assertEqual(r.rs.gate, 2)

    def test_skip_button(self):
        r = self.rig().start()
        r.to_quiz()
        card = r.panel._card
        card.skip.click()
        self.assertEqual(r.rs.skipped, {0})
        self.assertEqual(r.rs.results, [])                 # 不算对错
        self.assertIsNone(r.rs.showing)
        self.assertEqual(r.rs.gate, 1)
        self.assertIs(r.rs.widget.focusWidget(), r.view)
        # 读完下一节还会出下一节的题；第 0 节不再出
        r.bg.flush()
        for _ in range(60):
            if r.panel._card is not None:
                break
            r.view.page_forward()
        self.assertEqual(r.panel._card.quiz.section, 1)
        self.assertNotIn(0, [q.section for q in r.rs.shown_quizzes[1:]])

    def test_front_matter_gets_no_quiz(self):
        from reader.books.model import is_front_matter_title
        for t in ("目录", "目 录", "版权信息", "Contents", "封面"):
            self.assertTrue(is_front_matter_title(t), t)
        for t in ("导读", "第一章 导论", "前言"):
            self.assertFalse(is_front_matter_title(t), t)


# ── 2 · 跳到出处后的页码 ─────────────────────────────────────
class SourcePageTests(Case):
    def test_wrong_answer_page_label(self):
        r = self.rig().start()
        r.to_quiz()
        card = r.panel._card
        card._pick(0)                                      # 错
        card._button()
        here = r.view.source_label()
        self.assertTrue(here and page_number(here))
        self.assertEqual(card.cap2.text(), f"原文在{here}")
        self.assertIn(here, r.panel._context)             # 左栏「看得到」也是出处那页
        self.assertIsNone(r.panel._src_card)               # 卡只认跳过去后的第一次

    def test_layout_reemit_refreshes_context_and_strip(self):
        r = self.rig().start()
        loc = r.view.current_loc()
        sec = r.rs.book.section_at(loc)
        with mock.patch.object(r.view, "page_label", return_value="第 99 页"), \
                mock.patch.object(r.rs.strip, "set_book") as set_book:
            r.view.location_changed.emit(loc)               # EPUB 全书排完：位置没变，页码准了
        self.assertIn("第 99 页", r.panel._context)
        set_book.assert_called_with(r.entry.title, sec.title)

    def test_real_layout_finishes_and_matches(self):
        r = self.rig().start()
        pump(r.view.layout_done, 10)
        pump(timeout=0.1)
        label = r.view.page_label(r.view.current_loc())
        self.assertIn(label, r.panel._context)


# ── 3 · 焦点 / 6 · 正在打开 ──────────────────────────────────
class FocusAndLoadingTests(Case):
    def test_focus_goes_to_book_not_composer(self):
        r = self.rig().start(open_now=False)
        self.assertIn("正在打开", r.rs.area.loading.text())
        self.assertFalse(r.rs.area.loading.isHidden())
        self.assertIs(r.rs.widget.focusWidget(), r.rs.area)   # 书还没打开：先放在书页区
        r.open()
        self.assertTrue(r.rs.area.loading.isHidden())
        fw = r.rs.widget.focusWidget()
        self.assertIs(fw, r.view)
        self.assertIsNot(fw, r.panel.composer.input)

    def test_open_failed(self):
        def boom(path):
            raise OSError("坏文件")
        r = self.rig(open_book=boom).start()
        self.assertEqual(len(r.summaries), 1)
        s = r.summaries[0]
        self.assertEqual(s["reason"], "open_failed")
        self.assertIn("坏文件", s["error"])
        self.assertIsNone(s["rewrite_note"])


# ── 8 · 顶上两条不重叠 ───────────────────────────────────────
class StackTests(Case):
    def test_notice_goes_below_source_pill(self):
        r = self.rig().start()
        a = r.rs.area
        a.notice.show_text("刚才离开了 3 分 12 秒，这段没算进去，已记下")
        a._place()
        self.assertEqual(a.notice.y(), 14)
        r.to_quiz()
        card = r.panel._card
        card._pick(0)
        card._button()                                     # 跳到出处：书页顶上出小条
        pill = r.view.pill
        self.assertFalse(pill.isHidden())
        pill_bottom = pill.mapTo(a, pill.rect().bottomLeft()).y()
        if not a.notice.isHidden():                        # 跳页可能已经让「刚才离开了」走了
            self.assertGreater(a.notice.y(), pill_bottom)
        a.notice.show_text("刚才离开了 1 分钟")
        a._place()
        self.assertGreater(a.notice.y(), pill_bottom)
        r.view.clear_source()                              # 小条收起：回到顶上
        self.assertEqual(a.notice.y(), 14)


# ── 5 · 读完的流程 ───────────────────────────────────────────
class FinishTests(Case):
    def test_done_emits_now_note_written_later(self):
        r = self.rig(minutes=1).start()
        r.to_quiz()
        card = r.panel._card
        card._pick(0)
        card._button()
        card._pick(1)
        card._button()
        for _ in range(61):
            r.clock.advance(1)
            r.rs.tick()
        r.rs.finish()
        self.assertEqual(len(r.summaries), 1)
        s = r.summaries[0]
        self.assertEqual(s["reason"], "done")
        self.assertEqual(s["minutes"], 1)
        self.assertIsInstance(s["pages"], list)
        self.assertEqual(len(s["pages"]), 2)
        self.assertTrue(all(isinstance(p, int) for p in s["pages"]))
        self.assertLessEqual(s["pages"][0], s["pages"][1])
        self.assertEqual(len(s["quizzes"]), 1)
        self.assertIsInstance(s["quizzes"][0], Quiz)
        self.assertEqual(len(s["quizzes"][0].attempts), 2)
        self.assertTrue(s["note_pending"])
        self.assertNotIn("note", r.ai.calls)
        path = s["rewrite_note"]()
        self.assertTrue(Path(path).is_file())
        self.assertEqual(r.lib.saved_notes[-1][0], r.entry.id)

    def test_short_session_skips_note_but_can_write(self):
        # 读不到 3 分钟、没问没答：不自动写（省额度），但给按钮能写
        r = self.rig(minutes=1).start()
        for _ in range(61):
            r.clock.advance(1)
            r.rs.tick()
        r.rs.finish()
        s = r.summaries[0]
        self.assertEqual(s["reason"], "done")
        self.assertFalse(s.get("note_pending"))
        self.assertIn("不到 3 分钟", s["note_skipped"])
        self.assertTrue(callable(s["rewrite_note"]))
        self.assertEqual(r.ai.calls.count("note"), 0)

    def test_emergency_exit_writes_no_note(self):
        r = self.rig().start()
        r.rs.session.begin_emergency()
        for i in range(4):
            r.rs._exit_confirmed(i + 1, "有人找我" if i == 1 else None)
        self.assertEqual(len(r.summaries), 1)
        s = r.summaries[0]
        self.assertEqual(s["reason"], "emergency_exit")
        self.assertIsNone(s["rewrite_note"])
        self.assertFalse(s["note_pending"])
        self.assertEqual(r.ai.calls.count("note"), 0)


class NotesFlowTests(Case):
    """app.py + 书架 + 读书笔记页：笔记在写 → 写好；写不成；书打不开。"""

    def _ctl(self):
        from reader.app import ReaderApp
        from reader.ui.login import AuthResult
        from reader.ui.shelf import ShelfWindow
        from tests.test_books_ai import LONG, make_epub
        lib = Library(self.tmp / "lib")
        e = lib.add(make_epub(self.tmp / "书.epub", [("第一章", [LONG, LONG])]))

        class NoOcr:
            def state(self, bid):
                return None

            def busy(self):
                return False

            def enqueue(self, e):
                pass

            def resume_all(self):
                pass

            def stop(self, timeout=0):
                pass

        class Screen(QObject):
            finished = Signal(dict)
            made = []

            def __init__(self, entry, minutes, library):
                super().__init__()
                Screen.made.append(self)

            def start(self):
                pass

        w = ShelfWindow(lib, status_path=self.tmp / "status.json", check_login=False, ocr=NoOcr(),
                        prober=lambda: AuthResult(False))
        w.show()
        self.addCleanup(w.deleteLater)
        return lib, e, w, ReaderApp(w, lib, Screen), Screen

    def test_note_written_in_background(self):
        lib, e, w, ctl, Screen = self._ctl()
        gate = threading.Event()

        def rewrite():
            gate.wait(5)
            return lib.save_note(e.id, "# 《书》· 笔记\n\n## 这次读了什么\n\n- 一条\n", datetime.now())
        w.start_requested.emit(lib.get(e.id), 20)
        Screen.made[-1].finished.emit({"reason": "done", "minutes": 20, "pages": [3, 9], "quizzes": [],
                                       "note_path": None, "note_pending": True, "rewrite_note": rewrite})
        nv = w.notes_view
        self.assertIs(w.stack.currentWidget(), nv)
        self.assertTrue(nv.writing)
        self.assertFalse(nv.wait_box.isHidden())
        self.assertIn("AI 在写读书笔记", nv.wait_msg.text())
        self.assertTrue(nv.paper.isHidden())
        self.assertIn("写好了存进", nv.where.text())            # 还没有文件：不说「没有存下」
        self.assertFalse(nv.open_btn.isEnabled())
        gate.set()
        pump(lambda: not nv.writing, 5)
        self.assertFalse(nv.paper.isHidden())
        self.assertTrue(nv.wait_box.isHidden())
        self.assertIn("一条", nv.paper.toPlainText())
        self.assertIn("存在", nv.where.text())
        self.assertTrue(nv.open_btn.isEnabled())
        rec = lib.sessions()[-1]
        self.assertTrue(rec["summary"]["note_path"])
        self.assertIsNotNone(lib.session_for_note(Path(rec["summary"]["note_path"])))
        self.assertIn("第 3–9 页", nv.paper.toPlainText())

    def test_note_fails_then_retry(self):
        lib, e, w, ctl, Screen = self._ctl()
        tries = []

        def rewrite():
            tries.append(1)
            if len(tries) == 1:
                raise RuntimeError("额度用完了")
            return lib.save_note(e.id, "# 笔记\n\n- 第二次写成了\n", datetime.now())
        w.start_requested.emit(lib.get(e.id), 20)
        Screen.made[-1].finished.emit({"reason": "done", "minutes": 20, "note_pending": True,
                                       "rewrite_note": rewrite})
        nv = w.notes_view
        pump(lambda: not nv.writing, 5)
        self.assertFalse(nv.fail_box.isHidden())
        self.assertIn("额度用完了", nv.fail_msg.text())
        self.assertFalse(nv.retry_btn.isHidden())
        nv.retry_btn.click()
        pump(lambda: not nv.paper.isHidden(), 5)
        self.assertIn("第二次写成了", nv.paper.toPlainText())

    def test_open_failed_back_to_shelf(self):
        from reader.ui.shelf import Notice
        lib, e, w, ctl, Screen = self._ctl()
        w.start_requested.emit(lib.get(e.id), 20)
        Screen.made[-1].finished.emit({"reason": "open_failed", "error": "这本书打不开：坏了"})
        self.assertFalse(ctl.reading)
        self.assertIs(w.stack.currentWidget(), w.shelf_page)
        self.assertIn("这本书打不开", w.findChildren(Notice)[-1].text.text())
        self.assertEqual(lib.sessions(), [])               # 没开始计时，不记这一次


# ── 7 · 扫描版识别的进度 ─────────────────────────────────────
class OcrProgressTests(Case):
    def _pdf(self, pages=3) -> Path:
        doc = pymupdf.open()
        for _ in range(pages):
            doc.new_page().draw_rect(pymupdf.Rect(50, 50, 200, 200), color=(0, 0, 0))
        p = self.tmp / "scan.pdf"
        doc.save(p)
        return p

    def test_done_pages_reloads_with_one_stat(self):
        pdf = self._pdf()
        reader_side = ocr.OcrStore(pdf)
        writer = ocr.OcrStore(pdf)
        self.assertEqual(reader_side.done_pages(), 0)
        writer.put(0, {"text": "一", "words": []}, "zh-Hans-CN")
        writer.put(1, {"text": "二", "words": []}, "zh-Hans-CN")
        time.sleep(0.05)
        writer.save()
        calls = []
        real = Path.stat

        def counting(self, *a, **k):
            if self == reader_side.path:
                calls.append(1)
            return real(self, *a, **k)
        with mock.patch.object(Path, "stat", counting):
            self.assertEqual(reader_side.done_pages(), 2)       # 后台另一个对象写的，也数得到
            self.assertEqual(len(calls), 1)
            calls.clear()
            self.assertEqual(reader_side.done_pages(), 2)       # 没变：还是只看一眼
            self.assertEqual(len(calls), 1)

    def test_running_only_while_run_book_runs(self):
        pdf = self._pdf()
        seen = []

        def fake_page(page, lang):
            seen.append((ocr.is_running(pdf), ocr.OcrStore(pdf).running()))
            return {"text": "字" * 80, "words": []}
        self.assertFalse(ocr.is_running(pdf))
        with mock.patch.object(ocr, "ocr_page", fake_page), \
                mock.patch.object(ocr, "languages", lambda: ["zh-Hans-CN"]):
            store = ocr.run_book(pdf)
        self.assertTrue(seen)
        self.assertTrue(all(a and b for a, b in seen))
        self.assertFalse(ocr.is_running(pdf))
        self.assertFalse(store.running())
        self.assertEqual(store.done_pages(), 3)

    def test_queue_running_path(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self._pdf())
        gate, started = threading.Event(), threading.Event()

        def runner(path, *, title, progress, stop):
            started.set()
            gate.wait(5)
            return None
        q = OcrQueue(lib, runner=runner, available=True, counter=lambda p: 0)
        self.assertIsNone(q.running_path())
        q.enqueue(lib.get(e.id))
        self.assertTrue(started.wait(5))
        self.assertEqual(q.running_path(), e.path)
        gate.set()
        pump(lambda: not q.busy(), 5)
        self.assertIsNone(q.running_path())

    def test_bottom_bar_says_running_only_when_running(self):
        from reader.ui.bookview.pdf_view import PdfView
        v = PdfView()
        self.addCleanup(v.deleteLater)
        state = {"running": False, "done": 3}
        v.book = SimpleNamespace(text_source="ocr", ocr=object(), pages=10,
                                 ocr_progress=lambda: (state["done"], 10),
                                 ocr_running=lambda: state["running"])
        v._update_note()
        self.assertEqual(v.bar.note.text(), "扫描版 · 已识别 3 / 10 页")
        state["running"] = True
        v._update_note()
        self.assertIn("还在识别文字", v.bar.note.text())
        self.assertIn("3 / 10", v.bar.note.text())
        v.book.text_source, state["done"], state["running"] = "none", 0, False
        v._update_note()
        self.assertNotIn("还在识别", v.bar.note.text())
        self.assertIn("0 / 10", v.bar.note.text())
        state["running"] = True
        v._update_note()
        self.assertIn("还在识别文字", v.bar.note.text())


# ── 10 · 开发开关 ────────────────────────────────────────────
class NoLockSwitchTests(Case):
    def test_env_parsing(self):
        for v in ("1", "true", "YES", "on"):
            os.environ["READER_NO_LOCK"] = v
            self.assertTrue(no_lock(), v)
        for v in ("", "0", "no"):
            os.environ["READER_NO_LOCK"] = v
            self.assertFalse(no_lock(), v)

    def test_no_lock_uses_plain_window_and_no_overlays(self):
        os.environ["READER_NO_LOCK"] = "1"
        r = self.rig(locked=None).start()              # make_window / make_overlays 一调就报错
        self.assertFalse(r.rs.locked)
        self.assertIsNone(r.rs.window)
        self.assertIsNone(r.rs.overlays)
        self.assertTrue(r.rs.widget.isVisible())
        self.assertTrue(r.rs.session.active)            # 计时照常是真的
        self.assertIn("开发模式", r.rs.widget.windowTitle())

    def test_default_is_locked(self):
        r = self.rig(locked=None)
        self.assertTrue(r.rs.locked)

    def test_closing_dev_window_finishes(self):
        os.environ["READER_NO_LOCK"] = "1"
        r = self.rig(locked=None, logged_in=False).start()
        r.rs.widget.close()
        self.assertEqual(len(r.summaries), 1)
        self.assertEqual(r.summaries[0]["reason"], "done")


if __name__ == "__main__":
    unittest.main()
