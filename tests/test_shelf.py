"""书架这边（代理 A）：书架数据、开始读、登录块、书架窗口、读书笔记页、启动调度。

离线跑：不调真 AI、不开 PowerShell、不进锁屏。QT_QPA_PLATFORM=offscreen。
读书界面（D 写的 ReadingScreen）用这里的假件代替。

设 READER_LIVE_AUTH=1 会多跑一条：真的去问专用账号登录没有（只读）。
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402
from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from reader.books import Loc  # noqa: E402
from reader.library import BookEntry, Library, OcrQueue, book_id_for, clean_title, safe_name  # noqa: E402
from tests.test_books_ai import LONG, make_epub, make_pdf  # noqa: E402

app = QApplication.instance() or QApplication([])

from reader.ui import theme  # noqa: E402

theme.load_fonts()


def pump(until=None, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.01)
    if until is not None:
        raise AssertionError("等不到")


class TempData(unittest.TestCase):
    """每个测试一个临时数据目录（theme.data_dir / ocr 缓存都指过去），不碰真的书架。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reader-test-a-"))
        self._old = os.environ.get("READER_DATA_DIR")
        os.environ["READER_DATA_DIR"] = str(self.tmp / "data")
        theme.set_theme("dark", save=False)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("READER_DATA_DIR", None)
        else:
            os.environ["READER_DATA_DIR"] = self._old

    def epub(self, name="书.epub", chapters=3) -> Path:
        return make_epub(self.tmp / name, [(f"第{i + 1}章", [LONG, LONG]) for i in range(chapters)])

    def pdf(self, name="b.pdf", pages=12) -> Path:
        return make_pdf(self.tmp / name, pages, toc=[[1, "One", 1], [1, "Two", 6]])

    def scanned_pdf(self, name="scan.pdf", pages=4) -> Path:
        doc = pymupdf.open()
        for _ in range(pages):
            pg = doc.new_page()
            pg.draw_rect(pymupdf.Rect(50, 50, 200, 200), color=(0, 0, 0))   # 只有图形，没有文字层
        p = self.tmp / name
        doc.save(p)
        return p

    def status(self, week: list[int]) -> Path:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        days = {}
        for i, m in enumerate(week[:today.weekday() + 1]):
            d = monday + timedelta(days=i)
            days[d.strftime("%Y-%m-%d")] = {"credited_seconds": m * 60, "sessions": 1 if m else 0,
                                            "target_minutes": 45}
        p = self.tmp / "status.json"
        p.write_text(json.dumps({"days": days}), "utf-8")
        return p


# ── 数据 ────────────────────────────────────────────────────
class LibraryTests(TempData):
    def test_add_and_current(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        self.assertEqual(e.kind, "epub")
        self.assertEqual(e.title, "测试之书")
        self.assertEqual(e.author, "某作者")
        self.assertFalse(e.scanned)
        self.assertEqual(lib.current().id, e.id, "第一本自动成为在读")
        p = lib.add(self.pdf())
        self.assertEqual(p.kind, "pdf")
        self.assertEqual(p.pages, 12)
        self.assertEqual(lib.current().id, e.id, "加第二本不换在读")
        self.assertEqual(lib.add(self.pdf()).id, p.id, "同一个文件再加是同一本")
        self.assertEqual(len(lib.books()), 2)
        lib.set_current(p.id)
        self.assertEqual(Library(self.tmp / "lib").current().id, p.id, "存盘了")

    def test_refresh_structure_after_toc_fix(self):
        """拆书规则改了（目录纠错）：开书时换掉书架存的旧目录 / 小节；小节变了，按节号记的答题记号清掉。"""
        from reader.books import open_book
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        book = open_book(e.path)
        lib.mark_quiz_done(e.id, 1)
        lib.refresh_structure(e.id, book)                 # 没变：什么都不动
        self.assertEqual(lib.quiz_done(e.id), {1})
        extra = lib._extra(e.id)
        extra["sections"] = [["旧的节", 0, 0, 1]]
        extra["toc"] = [["指错了的一章", 0, 3, 1]]
        lib._save_extra(e.id, extra)
        self.assertEqual(lib.place(e.id, Loc(0, 5))["section"], "指错了的一章")
        lib.refresh_structure(e.id, book)
        self.assertEqual(lib.quiz_done(e.id), set())
        self.assertEqual(lib.place(e.id, Loc(0, 5))["section"], book.place_title(Loc(0, 5)))

    def test_rejects_other_files(self):
        lib = Library(self.tmp / "lib")
        bad = self.tmp / "x.txt"
        bad.write_text("hi")
        with self.assertRaises(ValueError):
            lib.add(bad)
        with self.assertRaises(ValueError):
            lib.add(self.tmp / "none.pdf")

    def test_position_progress_place(self):
        lib = Library(self.tmp / "lib")
        pdf = lib.add(self.pdf())
        self.assertIsNone(lib.progress(pdf.id))
        lib.save_position(pdf.id, Loc(4, 0))
        self.assertAlmostEqual(lib.progress(pdf.id), 5 / 12)
        self.assertEqual(lib.place(pdf.id)["section"], "One")
        lib.save_position(pdf.id, Loc(7, 0))
        self.assertEqual(lib.place(pdf.id)["section"], "Two")
        self.assertIsNotNone(lib.get(pdf.id).last_read)
        ep = lib.add(self.epub())
        lib.save_position(ep.id, Loc(1, 1))
        self.assertTrue(0 < lib.progress(ep.id) < 1)
        self.assertEqual(lib.place(ep.id)["section"], "第2章")
        # 最近读过的排前面
        self.assertEqual(lib.books()[0].id, ep.id)

    def test_highlights_quiz(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        items = [{"doc": 0, "block": 1, "start": 0, "end": 4, "text": "一段"}]
        lib.save_highlights(e.id, items)
        self.assertEqual(Library(self.tmp / "lib").highlights(e.id), items)
        lib.mark_quiz_done(e.id, 2)
        lib.mark_quiz_done(e.id, 0)
        lib.mark_quiz_done(e.id, 2)
        self.assertEqual(Library(self.tmp / "lib").quiz_done(e.id), {0, 2})

    def test_notes(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        when = datetime(2026, 9, 24, 14, 34)
        a = lib.save_note(e.id, "# 一", when)
        b = lib.save_note(e.id, "# 二", when + timedelta(minutes=5))
        self.assertEqual(a.name, "2026-09-24 第 1 次.md")
        self.assertEqual(b.name, "2026-09-24 第 2 次.md")
        self.assertEqual(a.parent.name, "测试之书")
        self.assertEqual(lib.notes(e.id), [b, a], "新的在前")
        self.assertEqual(lib.notes(), [b, a])
        self.assertEqual(a.read_text("utf-8"), "# 一")

    def test_remove_and_restore(self):
        lib = Library(self.tmp / "lib")
        src = self.epub()
        e = lib.add(src)
        lib.save_position(e.id, Loc(2, 0))
        lib.remove(e.id)
        self.assertEqual(lib.books(), [])
        self.assertIsNone(lib.current())
        self.assertTrue(src.exists(), "拿掉不删文件")
        back = lib.add(src)
        self.assertEqual(back.position, Loc(2, 0), "再加回来读到哪还在")
        self.assertEqual(lib.current().id, e.id)

    def test_missing_and_relink(self):
        lib = Library(self.tmp / "lib")
        src = self.epub()
        e = lib.add(src)
        moved = self.tmp / "moved.epub"
        src.rename(moved)
        self.assertTrue(lib.get(e.id).missing)
        lib.relink(e.id, moved)
        self.assertFalse(lib.get(e.id).missing)
        self.assertEqual(lib.get(e.id).id, e.id)

    def test_atomic_json(self):
        lib = Library(self.tmp / "lib")
        lib.add(self.epub())
        leftovers = [p for p in (self.tmp / "lib").rglob("*.tmp")]
        self.assertEqual(leftovers, [])
        json.loads((self.tmp / "lib" / "library.json").read_text("utf-8"))

    def test_sessions(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        note = lib.save_note(e.id, "# x", datetime.now())
        st = datetime.now().replace(microsecond=0)
        lib.log_session(e.id, {"minutes": 27, "note_path": note, "loc": Loc(1, 2), "cb": lambda: 1},
                        st, st + timedelta(minutes=29), section="3.2")
        rec = lib.session_for_note(note)
        self.assertEqual(rec["summary"]["minutes"], 27)
        self.assertEqual(rec["summary"]["loc"], [1, 2])
        self.assertNotIn("cb", rec["summary"], "函数不存")
        self.assertEqual(rec["section"], "3.2")
        self.assertEqual(len(lib.sessions(st.strftime("%Y-%m-%d"))), 1)

    def test_clean_title(self):
        stem = "测试之书_副标题 -- 某作者 -- 某丛书 #2, 2011 -- 某出版社 -- abcd -- Example Site"
        self.assertEqual(clean_title(stem, stem), ("测试之书：副标题", "某作者"))
        self.assertEqual(clean_title("", "Some Book_ The Guide -- K -- P -- x")[0],
                         "Some Book: The Guide")
        self.assertEqual(clean_title("测试之书", "whatever")[0], "测试之书")
        self.assertEqual(safe_name('a/b:c*?"'), "a b c")
        self.assertEqual(book_id_for("C:/A/b.pdf"), book_id_for("c:\\a\\B.PDF") if os.name == "nt"
                         else book_id_for("C:/A/b.pdf"))


class OcrQueueTests(TempData):
    def test_scanned_book_gets_recognized(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.scanned_pdf())
        self.assertTrue(e.scanned)
        gate = threading.Event()
        seen = []

        class Store:
            def done_pages(self):
                return 4

        def runner(path, *, title, progress, stop):
            progress(1, 4)
            seen.append("started")
            gate.wait(5)
            progress(4, 4)
            return Store()

        q = OcrQueue(lib, runner=runner, available=True, counter=lambda p: 0)
        self.assertEqual(q.state(e.id)[0], "paused")
        q.enqueue(e)
        pump(lambda: seen, 5)
        st = q.state(e.id)
        self.assertEqual(st[0], "running")
        self.assertEqual(st[1:], (1, 4))
        gate.set()
        pump(lambda: not q.busy(), 5)
        self.assertEqual(q.state(e.id)[0], "done")
        self.assertTrue(Library(self.tmp / "lib").get(e.id).ocr_done, "做完记下来，下次不再做")

    def test_unavailable_and_not_scanned(self):
        lib = Library(self.tmp / "lib")
        s = lib.add(self.scanned_pdf())
        t = lib.add(self.pdf())
        q = OcrQueue(lib, runner=lambda *a, **k: None, available=False)
        self.assertEqual(q.state(s.id)[0], "unavailable")
        self.assertIsNone(q.state(t.id))
        q.enqueue(lib.get(s.id))
        self.assertFalse(q.busy())

    def test_stop_keeps_partial(self):
        lib = Library(self.tmp / "lib")
        e = lib.add(self.scanned_pdf(pages=6))

        def runner(path, *, title, progress, stop):
            for i in range(6):
                if stop.is_set():
                    break
                progress(i + 1, 6)
                time.sleep(0.05)
            return None

        q = OcrQueue(lib, runner=runner, available=True, counter=lambda p: 0)
        q.enqueue(e)
        pump(lambda: q.state(e.id)[0] == "running", 5)
        q.stop(timeout=5)
        self.assertFalse(lib.get(e.id).ocr_done)


# ── 开始读 ──────────────────────────────────────────────────
class StartDialogTests(TempData):
    def test_logic(self):
        from reader.ui import start_dialog as sd
        self.assertEqual(sd.default_minutes(18), 27)
        self.assertEqual(sd.default_minutes(45), 15)
        self.assertEqual(sd.default_minutes(60), 15)
        self.assertEqual(sd.default_minutes(43), 5, "差 2 分钟也至少读 5 分钟")
        self.assertEqual([m for m, _ in sd.presets(18)], [15, 27, 45])
        self.assertIn("补满今天", dict(sd.presets(18))[27])
        self.assertEqual([m for m, _ in sd.presets(0)], [15, 45])
        self.assertEqual(sd.outcome(27, 18), "读完这次，今天正好满 45 分钟。")
        self.assertEqual(sd.outcome(15, 18), "读完今天 33 分钟，还差 12。")
        self.assertIn("加读", sd.outcome(15, 50))
        self.assertEqual(sd.check_minutes("30"), (30, ""))
        m, warn = sd.check_minutes("120")
        self.assertEqual(m, 120)
        self.assertIn("很长", warn)
        self.assertEqual(sd.check_minutes("200")[0], None)
        self.assertEqual(sd.check_minutes("3")[0], None)
        self.assertEqual(sd.check_minutes("")[0], None)

    def _dialog(self, today, ai=True):
        from PySide6.QtWidgets import QWidget

        from reader.ui.start_dialog import StartDialog
        lib = Library(self.tmp / "lib")
        e = lib.add(self.pdf())
        lib.save_position(e.id, Loc(6, 0))
        host = QWidget()
        host.resize(1280, 800)
        host.show()
        dlg = StartDialog(host, lib.get(e.id), lib, today_minutes=today, ai_ready=ai)
        self._keep = host
        return dlg

    def test_defaults_and_button(self):
        dlg = self._dialog(18)
        self.assertEqual(dlg.minutes(), 27)
        self.assertTrue(dlg.preset_buttons[27].isChecked())
        self.assertEqual(dlg.start_btn.text(), "开始读 27 分钟")
        self.assertIn("正好满 45", dlg.outcome.text())
        dlg.preset_buttons[15].click()
        self.assertEqual(dlg.start_btn.text(), "开始读 15 分钟")
        self.assertIn("还差 12", dlg.outcome.text())

    def test_extra_reading(self):
        dlg = self._dialog(47)
        self.assertEqual(dlg.minutes(), 15)
        self.assertEqual(dlg.start_btn.text(), "开始读 15 分钟")

    def test_custom_range_and_warning(self):
        dlg = self._dialog(18)
        dlg.custom.edit.setText("120")
        dlg._custom_edited("120")
        self.assertEqual(dlg.minutes(), 120)
        self.assertTrue(dlg.warn.isVisibleTo(dlg))
        self.assertTrue(dlg.start_btn.isEnabled())
        self.assertFalse(any(b.isChecked() for b in dlg.preset_buttons.values()))
        dlg.custom.edit.setText("500")
        dlg._custom_edited("500")
        self.assertIsNone(dlg.minutes())
        self.assertFalse(dlg.start_btn.isEnabled())
        self.assertEqual(dlg.start_btn.text(), "开始读")
        dlg.custom.edit.setText("")
        dlg._custom_edited("")
        self.assertEqual(dlg.minutes(), 27, "清空回到默认")

    def test_escape_cancels(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        dlg = self._dialog(18)
        QTimer.singleShot(50, lambda: QTest.keyClick(dlg, Qt.Key_Escape))
        self.assertEqual(dlg.exec(), 0)

    def test_enter_starts(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        dlg = self._dialog(18)
        QTimer.singleShot(50, lambda: QTest.keyClick(dlg.start_btn, Qt.Key_Return))
        self.assertEqual(dlg.exec(), 1)
        self.assertEqual(dlg.minutes(), 27)


# ── 登录块 ──────────────────────────────────────────────────
class LoginTests(TempData):
    def test_helpers(self):
        from reader.ui import login
        self.assertTrue(login.AuthResult(True, "A@x.com", "a@x.com").same_account)
        self.assertFalse(login.AuthResult(True, "a@x.com", "b@x.com").same_account)
        self.assertFalse(login.AuthResult(True, "a@x.com", "").same_account)
        self.assertEqual(login.mask_email("reader@example.com"), "r•••@example.com")
        args = login.powershell_args()
        self.assertEqual(args[0], "powershell.exe")
        script = base64.b64decode(args[-1]).decode("utf-16-le")
        self.assertIn(f"$env:CLAUDE_CONFIG_DIR='{login.claude.config_dir()}'", script)
        self.assertTrue(script.rstrip().endswith("auth login"))
        self.assertNotIn("logout", script)
        self.assertIn("auth logout", base64.b64decode(login.powershell_args(True)[-1]).decode("utf-16-le"))
        self.assertIn("claude auth login", login.manual_commands())
        self.assertEqual(login._ps_literal("D:/a'b/$(value)"), "'D:/a''b/$(value)'")

    def test_default_status_drops_env(self):
        from unittest import mock

        from reader.ui import login
        seen = {}

        def fake_run(cmd, **kw):
            seen["env"] = kw["env"]
            return mock.Mock(stdout='{"loggedIn": true, "email": "me@x.com"}')
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "X"}), \
                mock.patch.object(login.claude, "cli_path", return_value="claude.exe"), \
                mock.patch.object(login.subprocess, "run", fake_run):
            self.assertEqual(login.default_status()["email"], "me@x.com")
        self.assertNotIn("CLAUDE_CONFIG_DIR", seen["env"])

    def _banner(self, results, lib=None):
        from reader.ui.login import LoginBanner
        launched = []
        it = iter(results)
        b = LoginBanner(lib, prober=lambda: next(it), launcher=launched.append)
        return b, launched

    def test_flow_out_waiting_ok(self):
        from reader.ui.login import AuthResult
        b, launched = self._banner([AuthResult(False), AuthResult(False),
                                    AuthResult(True, "reader@x.com", "daily@x.com")])
        states = []
        b.state_changed.connect(states.append)
        b.recheck()
        pump(lambda: b.state == "out")
        self.assertTrue(b.isVisibleTo(b.parentWidget() or b) or not b.isHidden())
        self.assertFalse(b.logged_in())
        b._open_btn.click()
        self.assertEqual(len(launched), 1)
        self.assertEqual(b.state, "waiting")
        self.assertTrue(b._poll.isActive(), "等登录时自动查")
        b.recheck(quiet=True)
        pump(lambda: not b._busy)
        self.assertEqual(b.state, "waiting", "还没登好就接着等")
        b.recheck(quiet=True)
        pump(lambda: b.state == "ok_new")
        self.assertFalse(b._poll.isActive())
        self.assertTrue(b.logged_in())
        self.assertIn("r•••@x.com", b._s_body.text())
        b._s_second.click()                      # 知道了
        self.assertEqual(b.state, "ok")
        self.assertTrue(b.isHidden())
        self.assertEqual(b.status_line()[0], "green")

    def test_logged_in_at_start_stays_hidden(self):
        from reader.ui.login import AuthResult
        b, _ = self._banner([AuthResult(True, "reader@x.com", "daily@x.com")])
        b.recheck(quiet=True)
        pump(lambda: b.state != "checking")
        self.assertEqual(b.state, "ok")
        self.assertTrue(b.isHidden())

    def test_same_account(self):
        from reader.ui.login import AuthResult
        lib = Library(self.tmp / "lib")
        same = AuthResult(True, "me@x.com", "ME@x.com")
        b, launched = self._banner([same, same], lib)
        b.recheck()
        pump(lambda: b.state == "same")
        self.assertIn("抢同一份额度", b._s_body.text())
        b._s_primary.click()                     # 退出，换个账号登
        self.assertEqual(len(launched), 1)
        script = base64.b64decode(launched[0][-1]).decode("utf-16-le")
        self.assertIn("auth logout", script)
        b._set_state("same")
        b._s_second.click()                      # 就用这个
        self.assertEqual(b.state, "ok")
        self.assertEqual(lib.setting("auth_same_ok"), "me@x.com")
        b.recheck(quiet=True)
        pump(lambda: not b._busy)
        self.assertEqual(b.state, "ok", "确认过就不再问")

    def test_error(self):
        from reader.ui.login import AuthResult
        b, _ = self._banner([AuthResult(False, error="找不到 claude 命令（Claude Code 没装？）")])
        b.recheck()
        pump(lambda: b.state == "error")
        self.assertIn("没找到", b._s_body.text())

    @unittest.skipUnless(os.environ.get("READER_LIVE_AUTH") == "1", "设 READER_LIVE_AUTH=1 才真的去问")
    def test_live_dedicated_account(self):
        from reader.ui.login import probe
        r = probe()
        self.assertTrue(r.logged_in, r.error)
        self.assertTrue(r.email)
        self.assertFalse(r.same_account, "专用账号跟平常用的应该不是同一个")


# ── 书架窗口 ────────────────────────────────────────────────
class _NoOcr:
    def __init__(self):
        self.queued = []

    def state(self, bid):
        return None

    def busy(self):
        return False

    def enqueue(self, e):
        self.queued.append(e.id)

    def resume_all(self):
        pass

    def stop(self, timeout=0):
        pass


class ShelfTests(TempData):
    def _win(self, lib, week=(45, 47, 31, 18, 0, 0, 0)):
        from reader.ui.login import AuthResult
        from reader.ui.shelf import ShelfWindow
        w = ShelfWindow(lib, status_path=self.status(list(week)),
                        prober=lambda: AuthResult(True, "a@x.com", "b@x.com"), check_login=False, ocr=_NoOcr())
        w.resize(1280, 800)
        w.show()
        pump(timeout=0.05)
        return w

    def test_read_status(self):
        from reader.ui.shelf import read_status
        p = self.status([45, 47, 31, 18, 0, 0, 0])
        s = read_status(p)
        wd = date.today().weekday()
        self.assertEqual(s["weekday"], wd)
        self.assertEqual(s["today"], [45, 47, 31, 18, 0, 0, 0][wd])
        self.assertEqual(s["target"], 45)
        self.assertEqual(read_status(self.tmp / "none.json")["today"], 0)

    def test_empty_shows_dropzone(self):
        from reader.ui.shelf import DropZone
        w = self._win(Library(self.tmp / "lib"))
        self.assertEqual(len(w.findChildren(DropZone)), 1)
        self.assertIsNone(w.current_card)

    def test_rows_hover_and_set_current(self):
        lib = Library(self.tmp / "lib")
        a = lib.add(self.epub())
        b = lib.add(self.pdf())
        w = self._win(lib)
        self.assertEqual(w.current_card is not None, True)
        self.assertEqual([r.entry.id for r in w.rows], [b.id])
        r = w.rows[0]
        self.assertFalse(r.action.isEnabled(), "没移上去时「设为在读」不出现")
        r.set_hover(True)
        self.assertTrue(r.action.isEnabled())
        r.action.click()
        self.assertEqual(lib.current().id, b.id)
        self.assertEqual([x.entry.id for x in w.rows], [a.id])

    def test_scanned_row_says_so(self):
        from reader.ui.shelf import BookRow
        lib = Library(self.tmp / "lib")
        lib.add(self.epub())
        lib.add(self.scanned_pdf())
        w = self._win(lib)
        texts = " ".join(lbl.text() for r in w.findChildren(BookRow) for lbl in r.findChildren(type(w.ai_text)))
        self.assertIn("扫描版：不能划线，AI 看不到正文", texts, "识别不了时（假队列给 None）照原来的说法")

    def test_remove_and_undo(self):
        from reader.ui.shelf import Notice
        lib = Library(self.tmp / "lib")
        lib.add(self.epub())
        b = lib.add(self.pdf())
        w = self._win(lib)
        w.remove_book(lib.get(b.id))
        self.assertIsNone(lib.get(b.id))
        n = w.findChildren(Notice)[-1]
        self.assertIn("拿掉了", n.text.text())
        w._undo_remove(b)
        self.assertIsNotNone(lib.get(b.id))

    def test_add_books_in_background(self):
        lib = Library(self.tmp / "lib")
        w = self._win(lib)
        w.add_books([self.epub(), self.scanned_pdf()])
        pump(lambda: len(lib.books()) == 2 and not w._adding, 10)
        self.assertIsNotNone(lib.current())
        self.assertEqual(len(w.ocr.queued), 1, "扫描版加进来就去识别文字")
        w.add_books([self.tmp / "书.epub"])
        pump(lambda: not w._adding, 10)
        self.assertIn("已经在书架上了", w.hint.text())

    def test_start_request(self):
        from unittest import mock

        from reader.ui import start_dialog
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        w = self._win(lib, week=(18,) * 7)
        got = []
        w.start_requested.connect(lambda entry, m: got.append((entry.id, m)))
        with mock.patch.object(start_dialog.StartDialog, "exec", lambda self: start_dialog.StartDialog.Accepted):
            self.assertTrue(w.open_start_dialog())
        self.assertEqual(got, [(e.id, 27)], "默认补满今天（今天 18 分钟时是 27）")

    def test_theme_switch(self):
        lib = Library(self.tmp / "lib")
        lib.add(self.epub())
        w = self._win(lib)
        w.seg._btns["light"].click()
        try:
            self.assertEqual(theme.current(), "light")
            self.assertEqual(w.seg.value(), "light")
        finally:
            theme.set_theme("dark", save=False)

    def test_week_text(self):
        lib = Library(self.tmp / "lib")
        w = self._win(lib, week=(45, 47, 31, 18, 0, 0, 0))
        wd = date.today().weekday()
        if wd >= 3:
            self.assertTrue(w._week_text().startswith("读满 2 天"))


# ── 读书笔记页 ──────────────────────────────────────────────
class NotesViewTests(TempData):
    def test_formatters(self):
        from reader.ui import notes_view as nv
        self.assertEqual(nv.fmt_pages([132, 151]), ("132 → 151", "19 页"))
        self.assertEqual(nv.fmt_pages(["第 3 页", "第 9 页"]), ("3 → 9", "6 页"))
        self.assertIsNone(nv.fmt_pages(None))
        q = [{"attempts": [{"correct": True}]}, {"attempts": [{"correct": False}, {"correct": True}]}]
        self.assertEqual(nv.fmt_quizzes(q), ("2 题全对", "1 题是第二次"))
        self.assertEqual(nv.fmt_quizzes([{"attempts": [{"correct": False}, {"correct": False}]}]),
                         ("1 题对了 0 题", ""))
        self.assertEqual(nv.fmt_quizzes({"total": 3, "correct": 3, "second": 1}), ("3 题全对", "1 题是第二次"))
        self.assertEqual(nv.fmt_quizzes([]), ("这次没出题", ""))
        self.assertEqual(nv.fmt_count(4, "次"), ("4 次", ""))
        self.assertEqual(nv.fmt_count([1, 2, 3], "处"), ("3 处", ""))
        self.assertEqual(nv.split_title("# 标题\n\n## 小节\n- 一"), ("标题", "## 小节\n- 一"))
        self.assertEqual(nv.page_range([132, 151]), "第 132–151 页")

    def test_show_session_and_failure(self):
        from reader.ui.notes_view import NotesView
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        note = lib.save_note(e.id, "# 《测试之书》· 今天\n\n## 这次读了什么\n\n- **要点**：一句话。\n", datetime.now())
        v = NotesView(lib, today_fn=lambda: (45, 45))
        v.resize(1280, 800)
        st = datetime.now().replace(hour=14, minute=5)
        v.show_session(e, {"minutes": 27, "idle_deducted": 2, "pages": [1, 9], "quizzes": [], "asked": 1,
                           "highlights": 0, "note_path": str(note), "reason": "done"}, st, st + timedelta(minutes=29))
        text = v.paper.toPlainText()
        self.assertIn("《测试之书》· 今天", text)
        self.assertIn("这次读了什么", text)
        self.assertIn("27 分钟", text)
        self.assertIn("存在", v.where.text())
        self.assertTrue(v.open_btn.isEnabled())
        calls = []

        def rewrite():
            calls.append(1)
            return lib.save_note(e.id, "# 重写的\n\n- 好了\n", datetime.now())
        v.show_session(e, {"minutes": 27, "note_path": None, "rewrite_note": rewrite}, st, st)
        self.assertTrue(v.fail_box.isVisibleTo(v))
        self.assertFalse(v.open_btn.isEnabled())
        v.retry_btn.click()
        pump(lambda: calls and not v.fail_box.isVisibleTo(v), 5)
        self.assertIn("重写的", v.paper.toPlainText())

    def test_open_old_note_from_log(self):
        from reader.ui.notes_view import NotesView
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        note = lib.save_note(e.id, "# 旧笔记\n\n正文\n", datetime.now() - timedelta(days=2))
        st = datetime.now() - timedelta(days=2)
        lib.log_session(e.id, {"minutes": 12, "note_path": str(note)}, st, st + timedelta(minutes=12))
        v = NotesView(lib)
        v.show_note_file(note)
        self.assertIn("旧笔记", v.paper.toPlainText())


# ── 启动调度（ReadingScreen 用假件） ──────────────────────────
class FakeScreen(QObject):
    finished = Signal(dict)
    made = []

    def __init__(self, entry, minutes, library):
        super().__init__()
        self.entry, self.minutes, self.library = entry, minutes, library
        FakeScreen.made.append(self)

    def start(self):
        self.started = True


class AppTests(TempData):
    def _ctl(self, factory=FakeScreen):
        from reader.app import ReaderApp
        from reader.ui.login import AuthResult
        from reader.ui.shelf import ShelfWindow
        lib = Library(self.tmp / "lib")
        e = lib.add(self.epub())
        w = ShelfWindow(lib, status_path=self.status([0] * 7), check_login=False, ocr=_NoOcr(),
                        prober=lambda: AuthResult(False))
        w.show()
        return lib, e, w, ReaderApp(w, lib, factory)

    def test_done_flow(self):
        FakeScreen.made.clear()
        lib, e, w, ctl = self._ctl()
        w.start_requested.emit(lib.get(e.id), 27)
        scr = FakeScreen.made[-1]
        self.assertTrue(scr.started)
        self.assertEqual(scr.minutes, 27)
        self.assertTrue(ctl.reading)
        self.assertFalse(w.isVisible(), "读书时书架藏起来")
        ctl.on_message("show")                    # 读书中再打开一次 reader：不理
        self.assertFalse(w.isVisible())
        note = lib.save_note(e.id, "# 笔记\n\n- 一\n", datetime.now())
        scr.finished.emit({"minutes": 27, "note_path": str(note), "reason": "done", "pages": [1, 5]})
        self.assertFalse(ctl.reading)
        self.assertTrue(w.isVisible())
        self.assertIs(w.stack.currentWidget(), w.notes_view)
        self.assertEqual(lib.sessions()[-1]["summary"]["minutes"], 27)

    def test_emergency_flow(self):
        from reader.ui.shelf import Notice
        FakeScreen.made.clear()
        lib, e, w, ctl = self._ctl()
        w.start_requested.emit(lib.get(e.id), 20)
        FakeScreen.made[-1].finished.emit({"minutes": 8, "reason": "emergency_exit"})
        self.assertIs(w.stack.currentWidget(), w.shelf_page)
        self.assertIn("紧急出口", w.findChildren(Notice)[-1].text.text())

    def test_reading_screen_missing(self):
        from unittest import mock

        from reader import app as app_mod
        from reader.ui.shelf import Notice
        lib, e, w, ctl = self._ctl(factory=None)
        with mock.patch.object(app_mod, "load_reading_screen", return_value=(None, "ModuleNotFoundError: x")):
            w.start_requested.emit(lib.get(e.id), 20)
        self.assertFalse(ctl.reading)
        self.assertTrue(w.isVisible())
        self.assertIn("读书界面还没装好", w.findChildren(Notice)[-1].text.text())

    def test_screen_raises(self):
        from reader.ui.shelf import Notice

        class Broken(FakeScreen):
            def start(self):
                raise RuntimeError("坏了")
        lib, e, w, ctl = self._ctl(factory=Broken)
        w.start_requested.emit(lib.get(e.id), 20)
        self.assertFalse(ctl.reading)
        self.assertTrue(w.isVisible())
        self.assertIn("坏了", w.findChildren(Notice)[-1].text.text())

    def test_single_instance(self):
        from reader.app import SingleInstance, send_to_running
        name = f"reader-test-{os.getpid()}"
        got = []
        server = SingleInstance(got.append, name)
        self.assertFalse(send_to_running("show", name + "-nobody", 200))
        # 另起一个进程当「第二次启动」，这个进程转事件循环当已经在跑的那个
        import subprocess
        import sys
        code = ("from PySide6.QtCore import QCoreApplication; a = QCoreApplication([]); "
                f"from reader.app import send_to_running; print(send_to_running('show', {name!r}))")
        p = subprocess.Popen([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[1]),
                             stdout=subprocess.PIPE, text=True)
        pump(lambda: got and p.poll() is not None, 20)
        out, _ = p.communicate(timeout=5)
        self.assertEqual(out.strip(), "True")
        self.assertEqual(got, ["show"])
        pump(timeout=0.3)                  # 让对方断开的事件先处理完
        server.server.close()
        AppTests._keep_server = server     # 进程里一直留着（跟真用时一样），免得回收时还有事件在路上


if __name__ == "__main__":
    unittest.main()
