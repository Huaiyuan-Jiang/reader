"""读书界面（D）的离线测试：紧急出口的步骤和计时、发呆补回、离开优先、小测编排、时间到。

不开真窗口（QT_QPA_PLATFORM=offscreen）、不调真 AI（假 ask_once）、不进锁屏（假窗口 / 假遮罩），
时间全靠假时钟推进，一秒都不用真等。跑法：python -m unittest discover -s tests
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from reader.ai.claude import Result
from reader.ai.companion import NOTE_SYSTEM, QUIZ_SYSTEM, Companion

from tests.test_books_ai import LONG, make_epub
from tests.test_lock import Clock

app = QApplication.instance() or QApplication([])

from reader.ui import theme  # noqa: E402

theme.load_fonts()

from reader.ui import exit_flow as ef  # noqa: E402
from reader.ui.cards import away_notice, away_text  # noqa: E402
from reader.ui.overlay_text import AWAY, IDLE, READING, OverlayText, overlay_lines  # noqa: E402
from reader.ui.reading import QUIZ_WAIT, ReadingDeps, ReadingScreen  # noqa: E402
from reader.ui.stand_ins import FakeAiPanel, FakeBookView, MemoryLibrary  # noqa: E402
from reader.ui.strip import Strip, minutes_left  # noqa: E402

QUOTE = "这是一段用来凑长度的正文，讲的是惩罚方式怎样从公开的酷刑变成安静的监禁"


def quiz_json(answer: int = 1) -> str:
    return json.dumps({"question": "这一节说惩罚怎么变了？",
                       "options": ["变得更公开", "从酷刑变成监禁", "完全取消了", "交给了教会"],
                       "answer": answer, "hints": ["不对", "对", "不对", "不对"],
                       "explain": "原文就这么说。", "quote": QUOTE}, ensure_ascii=False)


class FakeAI:
    """按系统提示分辨：出题给题，写笔记给笔记；quiz_ok=False 就一直出坏题。"""

    def __init__(self, quiz_ok: bool = True):
        self.quiz_ok = quiz_ok
        self.calls: list[str] = []

    def ask_once(self, prompt, *, system, timeout=240, stop=None):
        if system == QUIZ_SYSTEM:
            self.calls.append("quiz")
            return Result(text=quiz_json() if self.quiz_ok else "不是 JSON")
        if system == NOTE_SYSTEM:
            self.calls.append("note")
            return Result(text="# 笔记\n## 这次读了什么\n- 一条")
        return Result(error="?")

    def run(self, prompt, **kw):
        yield Result(text="好", session_id="s")


class BgQueue:
    """代替线程池：先攒着，测试里 flush() 才「做完」。sync=True 就当场做完。"""

    def __init__(self, sync: bool = False):
        self.sync = sync
        self.jobs: list = []

    def __call__(self, fn, cb):
        if self.sync:
            cb(fn())
        else:
            self.jobs.append((fn, cb))

    def flush(self):
        while self.jobs:
            fn, cb = self.jobs.pop(0)
            try:
                r = fn()
            except Exception as e:  # noqa: BLE001
                r = e
            cb(r)


class FakeWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.events: list[str] = []

    def set_content_widget(self, w):
        w.setParent(self)
        w.resize(1280, 800)
        self.resize(1280, 800)
        self.events.append("content")

    def show_locked(self):
        self.events.append("show_locked")
        self.show()

    def close(self):
        self.events.append("close")
        return super().close()


class FakeOverlays:
    def __init__(self):
        self.texts: list[str] = []
        self.shown = self.closed = False

    def show(self):
        self.shown = True

    def set_text(self, t):
        self.texts.append(t)

    def close(self):
        self.closed = True


class FakeWatcher(QObject):
    left = Signal()
    returned = Signal()


def ensure_epub(tmp: Path) -> Path:
    paras = [LONG] * 5
    return make_epub(tmp / "book.epub", [("第一章", paras), ("第二章", paras), ("第三章", paras)])


class Rig:
    """一次读书的全套假零件。"""

    def __init__(self, tmp: Path, *, minutes=30, logged_in=True, quiz_ok=True, sync=False,
                 scanned=False, done=(), idle_timeout=300):
        self.clock = Clock(1_700_000_000.0)
        self.lib = MemoryLibrary(notes_dir=tmp / "notes")
        self.entry = self.lib.add(ensure_epub(tmp))
        self.entry.scanned = scanned
        for d in done:
            self.lib.mark_quiz_done(self.entry.id, d)
        self.ai = FakeAI(quiz_ok)
        self.bg = BgQueue(sync)
        self.window = FakeWindow()
        self.overlays = FakeOverlays()
        self.watcher = FakeWatcher()
        self.released = []
        self.panel = FakeAiPanel(sample_chat=False)
        deps = ReadingDeps(
            make_view=lambda book: FakeBookView(),
            make_panel=lambda: self.panel,
            make_companion=lambda book: Companion(book, runner=self.ai.run, ask_once=self.ai.ask_once),
            logged_in=lambda: logged_in,
            run_bg=self.bg,
            make_window=lambda: self.window,
            make_overlays=lambda: self.overlays,
            make_watcher=lambda: self.watcher,
            release_top=self.released.append,
            clock=self.clock,
            state_path=tmp / "session.json", status_path=tmp / "status.json",
            idle_timeout=idle_timeout)
        self.rs = ReadingScreen(self.entry, minutes, self.lib, deps=deps, locked=True)
        RIGS.append(self)
        self.summaries: list[dict] = []
        self.rs.finished.connect(self.summaries.append)

    def close(self):
        self.rs.dispose()
        self.window.deleteLater()

    def start(self):
        self.rs.start()
        # 打开书、查登录（两个后台活）
        for _ in range(2):
            if self.bg.jobs:
                fn, cb = self.bg.jobs.pop(0)
                cb(fn())
        return self

    @property
    def view(self) -> FakeBookView:
        return self.rs.view

    def advance(self, seconds: int, step: int = 1):
        for _ in range(0, seconds, step):
            self.clock.advance(step)
            self.rs.tick()

    def barrier_calls(self):
        return [c[1] for c in self.view.calls if c[0] == "set_barrier"]


RIGS: list = []


def turn_until_barrier(r, limit=40) -> bool:
    for _ in range(limit):
        if r.view.blocked:
            return True
        r.view.turn(1)
    return r.view.blocked


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        theme.set_theme("dark", save=False)

    def tearDown(self):
        while RIGS:
            RIGS.pop().close()
        if getattr(self, "host", None) is not None:
            self.host.deleteLater()
            self.host = None
        QApplication.processEvents()
        self._tmp.cleanup()


# ── 紧急出口：状态机和计时 ─────────────────────────────────
class TestExitFlow(TmpCase):
    def _flow(self):
        self.clock = Clock(100.0)
        self.host = QWidget()
        self.host.resize(1280, 800)
        self.host.show()
        flow = ef.ExitFlow(self.host, clock=self.clock,
                           facts=lambda: {"read": 11, "left": 16, "today": 29, "target": 45, "week_exits": 0})
        self.confirmed, self.stayed = [], []
        flow.confirmed.connect(lambda n, r: self.confirmed.append((n, r)))
        flow.stayed.connect(lambda: self.stayed.append(1))
        flow.open()
        QApplication.processEvents()
        return flow

    def _center(self, flow, btn):
        return btn.mapTo(flow.card, btn.rect().center())

    def test_four_steps_timings_and_positions(self):
        flow = self._flow()
        seen_texts = []
        # 第 1 步：「走」在左下，1 秒内不响应
        self.assertEqual(flow.step, 1)
        self.assertEqual(flow.leave_position(), "bottom-left")
        lv, st = self._center(flow, flow.leave_btn), self._center(flow, flow.stay_btn)
        self.assertLess(lv.x(), st.x())
        self.assertGreater(lv.y(), flow.CARD_H * 0.7)
        seen_texts.append(flow.leave_btn.text())
        self.clock.advance(0.5)
        flow.leave_btn.click()
        self.assertEqual(flow.step, 1)            # 太快，没反应
        self.clock.advance(0.5)
        flow.leave_btn.click()
        self.assertEqual(flow.step, 2)
        self.assertEqual(self.confirmed, [(1, None)])
        QApplication.processEvents()
        # 第 2 步：「走」在右下；不选原因点不了
        self.assertEqual(flow.leave_position(), "bottom-right")
        lv, st = self._center(flow, flow.leave_btn), self._center(flow, flow.stay_btn)
        self.assertGreater(lv.x(), st.x())
        seen_texts.append(flow.leave_btn.text())
        self.clock.advance(2)
        self.assertFalse(flow.leave_btn.isEnabled())
        flow.leave_btn.click()
        self.assertEqual(flow.step, 2)
        flow.pick_reason("要出门")
        self.assertTrue(flow.leave_btn.isEnabled())
        flow.leave_btn.click()
        self.assertEqual(flow.step, 3)
        self.assertEqual(self.confirmed[-1], (2, "要出门"))
        QApplication.processEvents()
        # 第 3 步：「走」是上面那个，要等 5 秒，按钮上倒数
        self.assertEqual(flow.leave_position(), "top")
        lv, st = self._center(flow, flow.leave_btn), self._center(flow, flow.stay_btn)
        self.assertLess(lv.y(), st.y())
        self.assertFalse(flow.leave_btn.isEnabled())
        self.assertIn("5", flow.leave_btn.text())
        self.clock.advance(1.2)
        flow.refresh()
        self.assertFalse(flow.leave_btn.isEnabled())
        self.assertIn("4", flow.leave_btn.text())
        self.clock.advance(3.7)                     # 4.9 秒
        flow.refresh()
        self.assertFalse(flow.can_leave())
        self.clock.advance(0.1)                     # 5 秒整
        flow.refresh()
        self.assertTrue(flow.leave_btn.isEnabled())
        seen_texts.append(flow.leave_btn.text())
        flow.leave_btn.click()
        self.assertEqual(flow.step, 4)
        QApplication.processEvents()
        # 第 4 步：「走」在中间
        self.assertEqual(flow.leave_position(), "center")
        lv = self._center(flow, flow.leave_btn)
        self.assertAlmostEqual(lv.x(), flow.CARD_W / 2, delta=4)
        seen_texts.append(flow.leave_btn.text())
        self.clock.advance(0.9)
        flow.leave_btn.click()
        self.assertEqual(flow.step, 4)
        self.clock.advance(0.1)
        flow.leave_btn.click()
        self.assertEqual(self.confirmed[-1], (4, None))
        self.assertFalse(flow.active)
        self.assertEqual(len(set(seen_texts)), 4)    # 每一步说法都不同
        self.assertEqual(self.stayed, [])

    def test_escape_means_stay_at_any_step(self):
        for target in (1, 2, 3, 4):
            flow = self._flow()
            flow.goto(target)
            QTest.keyClick(flow, Qt.Key_Escape)
            self.assertFalse(flow.active, target)
            self.assertEqual(self.stayed, [1])
            self.assertEqual(self.confirmed, [])

    def test_enter_never_leaves(self):
        flow = self._flow()
        for target in (1, 2, 3, 4):
            flow.goto(target)
            if target == 2:
                flow.pick_reason("有人找我")
            self.clock.advance(10)
            flow.refresh()
            for key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                QTest.keyClick(flow, key)
            self.assertEqual(flow.step, target)
            self.assertEqual(self.confirmed, [])
            self.assertEqual(flow.leave_btn.focusPolicy(), Qt.NoFocus)   # 只能用鼠标点

    def test_stay_button_also_inert_for_a_second(self):
        flow = self._flow()
        flow.stay_btn.click()
        self.assertTrue(flow.active)
        self.clock.advance(1)
        flow.stay_btn.click()
        self.assertFalse(flow.active)
        self.assertEqual(self.stayed, [1])


# ── 发呆：补回那 5 分钟 ──────────────────────────────────────
class TestIdle(TmpCase):
    def test_idle_card_and_credit_back(self):
        r = Rig(self.tmp, logged_in=False).start()
        a = r.rs.area
        r.advance(100)
        r.view.turn(1)                       # 100 秒时翻了一页
        r.advance(301)
        self.assertTrue(a.idle_card.isVisible())
        self.assertTrue(r.view.dimmed)
        self.assertEqual(r.rs.mode(), "pause")
        self.assertEqual(r.rs.strip.state, "pause")
        self.assertIn("阅读暂停了", r.overlays.texts[-1].replace(" ", ""))
        self.assertAlmostEqual(r.rs.session.timer.credited, 100, delta=1)   # 先扣下
        r.advance(60)
        a.idle_card.button.click()           # 点「在读」
        self.assertFalse(a.idle_card.isVisible())
        self.assertFalse(r.view.dimmed)
        self.assertAlmostEqual(r.rs.session.timer.credited, 400, delta=1)   # 补回来了
        self.assertEqual(r.rs.strip.state, "run")
        self.assertIn("正在读书", r.overlays.texts[-1].replace(" ", ""))

    def test_page_turn_also_resumes(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.advance(301)
        self.assertTrue(r.rs.area.idle_card.isVisible())
        r.view.turn(1)
        self.assertFalse(r.rs.area.idle_card.isVisible())
        self.assertAlmostEqual(r.rs.session.timer.credited, 300, delta=1)

    def test_idle_card_never_takes_focus(self):
        r = Rig(self.tmp, logged_in=False).start()
        self.assertEqual(r.rs.area.idle_card.button.focusPolicy(), Qt.NoFocus)
        self.assertEqual(r.rs.area.leave_card.button.focusPolicy(), Qt.NoFocus)
        self.assertEqual(r.rs.strip.exit_btn.focusPolicy(), Qt.NoFocus)


# ── 离开：优先于发呆、回来后留一行 ─────────────────────────
class TestLeave(TmpCase):
    def test_leave_over_idle_then_back_notice(self):
        r = Rig(self.tmp, logged_in=False).start()
        a = r.rs.area
        r.advance(301)
        self.assertTrue(a.idle_card.isVisible())
        r.watcher.left.emit()                # 按了 Win 键
        self.assertFalse(a.idle_card.isVisible())
        self.assertTrue(a.leave_card.isVisible())
        self.assertEqual(r.rs.strip.state, "away")
        self.assertIn("今天第 1 次", a.leave_card.count_line.text())
        self.assertIn("回主屏点一下阅读窗口", r.overlays.texts[-1])
        r.advance(192)
        r.watcher.returned.emit()            # 点了一下窗口
        self.assertFalse(a.leave_card.isVisible())
        self.assertFalse(a.idle_card.isVisible())
        self.assertFalse(r.view.dimmed)
        self.assertTrue(a.notice.isVisible())
        self.assertEqual(a.notice.text.text(), "刚才离开了 3 分 12 秒，这段没算进去，已记下")
        # 发呆没人应就离开了：那 5 分钟不补
        self.assertAlmostEqual(r.rs.session.timer.credited, 0, delta=1)
        # 这行留着，翻页才走
        r.advance(30)
        self.assertTrue(a.notice.isVisible())
        r.view.turn(1)
        self.assertFalse(a.notice.isVisible())

    def test_no_idle_card_while_away(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.watcher.left.emit()
        r.advance(600)
        self.assertFalse(r.rs.area.idle_card.isVisible())
        self.assertTrue(r.rs.area.leave_card.isVisible())

    def test_notice_close_button(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.watcher.left.emit()
        r.advance(5)
        r.watcher.returned.emit()
        r.rs.area.notice.close_btn.click()
        self.assertFalse(r.rs.area.notice.isVisible())

    def test_away_text(self):
        self.assertEqual(away_text(45), "45 秒")
        self.assertEqual(away_text(192), "3 分 12 秒")
        self.assertEqual(away_text(3900), "1 小时 5 分")
        self.assertIn("没算进去", away_notice(10))


# ── 小测编排 ────────────────────────────────────────────────
class TestQuizFlow(TmpCase):
    def test_prefetch_barrier_show_jump_finish(self):
        r = Rig(self.tmp).start()
        self.assertEqual(r.barrier_calls()[-1], 0)            # 挡在第 0 节末尾
        self.assertEqual(len(r.bg.jobs), 1)                   # 后台在出第 0 节的题
        self.assertTrue(turn_until_barrier(r))
        self.assertNotIn(("show_quiz", 0), r.panel.calls)     # 题还没出好：先挡着，不显示空卡
        r.bg.flush()
        self.assertIn(("show_quiz", 0), r.panel.calls)        # 出好了，马上显示
        r.panel.pick(0)                                       # 选错
        r.panel.confirm()
        marks = [c for c in r.view.calls if c[0] == "mark_source"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0][2], QUOTE)                  # 跳到原文并标出来
        r.panel.pick(1)                                       # 第二次对了
        r.panel.confirm()
        self.assertIn(("mark_quiz_done", r.entry.id, 0), r.lib.calls)
        self.assertEqual(r.barrier_calls()[-1], 1)            # 挪到下一节
        self.assertIn(("clear_source",), r.view.calls)
        self.assertEqual(len(r.bg.jobs), 1)                   # 下一节的题开始出
        self.assertEqual(r.rs.results, [{"section": 0, "title": "第一章", "correct": True, "tries": 2}])

    def test_done_sections_skipped(self):
        r = Rig(self.tmp, done=(0,)).start()
        self.assertEqual(r.barrier_calls()[-1], 1)
        r.bg.flush()
        self.assertEqual(r.ai.calls, ["quiz"])                # 只出了第 1 节的，第 0 节没出

    def test_not_logged_in_never_blocks(self):
        r = Rig(self.tmp, logged_in=False).start()
        self.assertTrue(all(b is None for b in r.barrier_calls()))
        self.assertFalse(turn_until_barrier(r))
        self.assertEqual(r.ai.calls, [])
        self.assertIn(("set_companion", False), r.panel.calls)

    def test_scanned_never_blocks(self):
        r = Rig(self.tmp, scanned=True).start()
        self.assertTrue(all(b is None for b in r.barrier_calls()))
        self.assertEqual(r.bg.jobs, [])

    def test_quiz_failure_releases_barrier(self):
        r = Rig(self.tmp, quiz_ok=False).start()
        self.assertEqual(r.barrier_calls()[-1], 0)
        r.bg.flush()                                          # 出题失败
        self.assertNotEqual(r.barrier_calls()[-1], 0)
        self.assertIn(0, r.rs.failed)
        self.assertNotIn(("show_quiz", 0), r.panel.calls)

    def test_slow_quiz_stops_blocking(self):
        r = Rig(self.tmp).start()
        self.assertTrue(turn_until_barrier(r))
        r.advance(int(QUIZ_WAIT) + 1)
        self.assertNotEqual(r.barrier_calls()[-1], 0)         # 等太久：放行
        r.bg.flush()                                          # 迟到的题不再弹
        self.assertNotIn(("show_quiz", 0), r.panel.calls)

    def test_going_back_keeps_barrier(self):
        r = Rig(self.tmp, done=()).start()
        r.bg.flush()
        before = r.barrier_calls()
        self.assertEqual(before[-1], 0)
        for _ in range(3):
            r.view.turn(1)
        for _ in range(3):
            r.view.turn(-1)
        self.assertEqual(r.barrier_calls(), before)


# ── 时间到 ─────────────────────────────────────────────────
class TestTimeUp(TmpCase):
    def test_time_up_release_more_finish_with_note(self):
        r = Rig(self.tmp, minutes=1, sync=False).start()
        r.bg.flush()                                           # 第 0 节的题出好
        self.assertTrue(turn_until_barrier(r))
        self.assertIn(("show_quiz", 0), r.panel.calls)
        r.advance(60)
        a = r.rs.area
        # 不踢人：撤置顶、撤遮罩、出「读够了」条、不再挡、题上写「答不答都行」
        self.assertEqual(r.released, [r.window])
        self.assertTrue(r.overlays.closed)
        self.assertTrue(a.time_up.isVisible())
        self.assertIn("1 分钟读够了", a.time_up.title.text())
        self.assertEqual(r.rs.strip.state, "done")
        self.assertIsNone(r.barrier_calls()[-1])
        self.assertIn(("quiz_timeout_note",), r.panel.calls)
        self.assertEqual(r.summaries, [])
        self.assertEqual(r.window.events.count("close"), 0)
        # 再读一会儿：多读的算进今天
        a.time_up.more_btn.click()
        self.assertFalse(a.time_up.isVisible())
        self.assertEqual(r.rs.strip.state, "extra")
        for _ in range(120):
            r.clock.advance(1)
            r.view.turn(1) if _ % 60 == 0 else None
            r.rs.tick()
        self.assertEqual(int(r.rs.session.extra_seconds // 60), 2)
        # 顶条「结束」→ 马上 finished（不等笔记）→ 笔记由 rewrite_note() 写、存进 Library
        r.rs.strip.finish_btn.click()
        self.assertEqual(len(r.summaries), 1)
        self.assertIn("close", r.window.events)
        s = r.summaries[0]
        self.assertEqual(s["reason"], "done")
        self.assertEqual(s["minutes"], 3)
        self.assertEqual(s["extra_minutes"], 2)
        self.assertEqual(s["today"]["credited_minutes"], 3)
        self.assertTrue(s["note_pending"])
        self.assertIsNone(s["note_path"])
        self.assertNotIn("note", r.ai.calls)                   # 还没开始写
        self.assertTrue(callable(s["rewrite_note"]))
        path = s["rewrite_note"]()
        self.assertEqual(r.ai.calls.count("note"), 1)
        self.assertTrue(Path(path).is_file())
        self.assertIn("save_note", [c[0] for c in r.lib.calls])
        # 「重新写一次」再调一次：又写一篇新的
        self.assertNotEqual(s["rewrite_note"](), path)

    def test_time_up_finish_without_ai(self):
        r = Rig(self.tmp, minutes=1, logged_in=False, sync=True).start()
        r.advance(61)
        r.rs.area.time_up.finish_btn.click()
        self.assertEqual(len(r.summaries), 1)
        self.assertIsNone(r.summaries[0]["note_path"])
        self.assertTrue(r.summaries[0]["note_error"])
        self.assertFalse(r.summaries[0]["note_pending"])
        self.assertIsNone(r.summaries[0]["rewrite_note"])


# ── 紧急出口：整屏走一遍 ───────────────────────────────────
class TestEmergencyWhole(TmpCase):
    def test_exit_records_reason_and_finishes(self):
        r = Rig(self.tmp, logged_in=False).start()
        for _ in range(10):
            r.view.turn(1)
            r.advance(60)
        flow = r.rs.exit_flow
        r.rs.strip.exit_btn.click()
        self.assertTrue(flow.active)
        self.assertEqual(r.rs.session.emergency.count, 0)
        r.clock.advance(1)
        flow.leave_btn.click()
        r.clock.advance(1)
        flow.pick_reason("身体不舒服")
        flow.leave_btn.click()
        r.clock.advance(5)
        flow.refresh()
        flow.leave_btn.click()
        self.assertEqual(r.summaries, [])
        r.clock.advance(1)
        flow.leave_btn.click()
        self.assertEqual(len(r.summaries), 1)
        s = r.summaries[0]
        self.assertEqual(s["reason"], "emergency_exit")
        self.assertEqual(s["exit_reason"], "身体不舒服")
        self.assertEqual(s["minutes"], 10)
        self.assertEqual(s["today"]["emergency_exits"], 1)
        self.assertEqual(s["today"]["emergency_log"][0]["reason"], "身体不舒服")
        self.assertIn("close", r.window.events)

    def test_esc_on_screen_does_not_open_exit(self):
        r = Rig(self.tmp, logged_in=False).start()
        QTest.keyClick(r.rs.widget, Qt.Key_Escape)
        self.assertFalse(r.rs.exit_flow.active)

    def test_stay_cancels(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.rs.strip.exit_btn.click()
        r.clock.advance(1)
        r.rs.exit_flow.leave_btn.click()
        QTest.keyClick(r.rs.exit_flow, Qt.Key_Escape)
        self.assertFalse(r.rs.exit_flow.active)
        self.assertEqual(r.rs.session.emergency.count, 0)
        self.assertTrue(r.rs.session.active)


# ── 保存、顶条、副屏 ───────────────────────────────────────
class TestSaveStripOverlay(TmpCase):
    def test_position_and_highlights_saved(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.view.turn(1)
        r.rs.save_position()
        self.assertEqual(r.lib.positions[r.entry.id], r.view.current_loc())
        r.view._hls = [{"doc": 0, "block": 1, "start": 0, "end": 4, "text": "这是一段"}]
        r.view.highlights_changed.emit()
        self.assertEqual(r.lib.highlights(r.entry.id)[0]["text"], "这是一段")

    def test_locked_start(self):
        r = Rig(self.tmp, logged_in=False).start()
        self.assertEqual(r.window.events[:2], ["content", "show_locked"])
        self.assertTrue(r.overlays.shown)

    def test_minutes_only_and_hover_seconds(self):
        self.assertEqual(minutes_left(18 * 60 + 1), 19)
        self.assertEqual(minutes_left(60), 1)
        self.assertEqual(minutes_left(0), 0)
        s = Strip()
        s.resize(1280, 40)
        s.set_times(18 * 60 + 42, 600, 1800, 1500)
        self.assertEqual([p[0] for p in s.clock._parts()], ["这次还剩", "19", "分钟"])
        s.clock.hovered = True
        self.assertEqual([p[0] for p in s.clock._parts()], ["这次还剩", "18", "分", "42", "秒"])
        s.clock.hovered = False
        s.set_state("pause")
        self.assertEqual(s.clock._parts()[0][0], "暂停")
        s.set_state("done")
        self.assertFalse(s.exit_btn.isVisibleTo(s))
        self.assertTrue(s.free.isVisibleTo(s))
        s.set_state("extra")
        self.assertTrue(s.finish_btn.isVisibleTo(s))

    def test_overlay_updates_once_a_minute(self):
        class T:
            def __init__(self):
                self.n = 0

            def set_text(self, _t):
                self.n += 1
        t = T()
        ot = OverlayText(t)
        for sec in range(16 * 60, 14 * 60 - 1, -1):  # 两分钟，每秒调一次
            ot.update(READING, sec)
        self.assertEqual(t.n, 3)                   # 16、15、14 各一次
        ot.update(IDLE, 14 * 60)
        self.assertEqual(t.n, 4)                   # 状态变了马上换
        self.assertEqual(overlay_lines(AWAY, 16 * 60)["hint"], "回主屏点一下阅读窗口，就接着读")
        self.assertEqual(overlay_lines(READING, 16 * 60)["minutes"], 16)

    def test_theme_switch_does_not_crash(self):
        r = Rig(self.tmp, logged_in=False).start()
        r.advance(301)
        theme.set_theme("light", save=False)
        QApplication.processEvents()
        theme.set_theme("dark", save=False)
        self.assertTrue(r.rs.area.idle_card.isVisible())


if __name__ == "__main__":
    unittest.main()
