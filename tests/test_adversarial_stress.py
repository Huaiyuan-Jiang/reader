"""Adversarial regressions using synthetic local data only.

Run with READER_NO_LOCK=1 and QT_QPA_PLATFORM=offscreen. These tests never
start the UI or contact a model/OCR service.
"""
from __future__ import annotations

import json
import os
import ctypes
import gc
import multiprocessing
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
import weakref

import pymupdf
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

os.environ.setdefault("READER_NO_LOCK", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from reader.books import ocr
from reader.books import Loc, open_book
from reader.app import ReaderApp
from reader.library import Library
from reader.ui import theme
from reader.ui.bookview.epub_view import EpubView
from reader.ui.bookview.pdf_view import PdfView
from tests.test_books_ai import LONG, make_epub, make_pdf
from tests.test_integration import Rig

app = QApplication.instance() or QApplication([])
theme.load_fonts()


def _working_set_mb() -> float | None:
    """Current Windows process working set; return None on non-Windows hosts."""
    if os.name != "nt":
        return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                    ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t),
                    ("peak_paged", ctypes.c_size_t), ("paged", ctypes.c_size_t),
                    ("peak_nonpaged", ctypes.c_size_t), ("nonpaged", ctypes.c_size_t),
                    ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.restype = ctypes.c_void_p
    read_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    read_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    read_memory.restype = ctypes.c_int
    ok = read_memory(get_process(), ctypes.byref(counters), counters.cb)
    return counters.ws / (1024 * 1024) if ok else None


class OcrStoreConcurrencyStress(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="reader-ocr-stress-")
        self.addCleanup(self._tmp.cleanup)
        self._old_data_dir = os.environ.get("READER_DATA_DIR")
        os.environ["READER_DATA_DIR"] = str(Path(self._tmp.name) / "data")
        self.addCleanup(self._restore_data_dir)
        self.pdf = Path(self._tmp.name) / "synthetic.pdf"
        # file_key only needs stable bytes here; OCR is not invoked.
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "synthetic page zero")
        doc.new_page().insert_text((72, 72), "synthetic page one")
        doc.save(self.pdf)
        doc.close()

    def _restore_data_dir(self):
        if self._old_data_dir is None:
            os.environ.pop("READER_DATA_DIR", None)
        else:
            os.environ["READER_DATA_DIR"] = self._old_data_dir

    def test_two_ocr_workers_do_not_overwrite_each_others_pages(self):
        """Separate run_book workers must retain both pages in the OCR file.

        Let both calls reach the mocked OCR engine before either processes its
        assigned page. The gate is before saving, so a per-book save lock can
        serialize writes without blocking this test.
        """
        result_path = ocr.OcrStore(self.pdf).path
        worker_gate = threading.Barrier(2)
        first_call = threading.local()

        errors: list[BaseException] = []
        results: list[ocr.OcrStore] = []

        def recognize(page):
            try:
                results.append(ocr.run_book(self.pdf, pages=[page], save_every=50))
            except BaseException as exc:  # captured to assert the worker outcome
                errors.append(exc)

        def fake_ocr(page, _lang):
            # Let both run_book calls reach OCR before either starts processing
            # its assigned page. With the fixed per-book lock this gate remains
            # outside the critical section, so the regression does not deadlock.
            if not getattr(first_call, "waited", False):
                first_call.waited = True
                worker_gate.wait(timeout=5)
            return {"text": f"synthetic page {page.number}", "words": []}

        with mock.patch.object(ocr, "ocr_page", fake_ocr), \
                mock.patch.object(ocr, "languages", lambda: ["en-US"]):
            workers = [threading.Thread(target=recognize, args=(page,)) for page in (0, 1)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=8)

        self.assertFalse(any(worker.is_alive() for worker in workers), "OCR save worker hung")
        self.assertEqual(errors, [], f"OCR workers failed: {errors!r}")
        raw = result_path.read_text("utf-8")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(f"concurrent OCR saves left invalid JSON ({exc}): {raw!r}")
        self.assertEqual(set(result["pages"]), {"0", "1"})


class ContinuousUseStress(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="reader-continuous-stress-")
        self.addCleanup(self._tmp.cleanup)
        self._old_data_dir = os.environ.get("READER_DATA_DIR")
        self.data_dir = Path(self._tmp.name) / "data"
        os.environ["READER_DATA_DIR"] = str(self.data_dir)
        self.addCleanup(self._restore_data_dir)

    def _restore_data_dir(self):
        if self._old_data_dir is None:
            os.environ.pop("READER_DATA_DIR", None)
        else:
            os.environ["READER_DATA_DIR"] = self._old_data_dir

    def test_shelf_focus_callback_is_safe_after_window_destruction(self):
        """A queued focus restore must tolerate its window being deleted first."""
        from reader.ui.shelf import ShelfWindow

        callback_after_destroy = []
        state = {"destroyed": False}

        class TrackedShelfWindow(ShelfWindow):
            def _restore_focus(self):
                if state["destroyed"]:
                    callback_after_destroy.append(True)
                    return
                super()._restore_focus()

        library = Library(Path(self._tmp.name) / "shelf-data")
        window = TrackedShelfWindow(library, status_path=Path(self._tmp.name) / "status.json",
                                    check_login=False)
        window.ocr.stop()
        window.destroyed.connect(lambda *_: state.update(destroyed=True))
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(callback_after_destroy, [],
                         "queued focus restoration ran after its ShelfWindow was destroyed")

    def test_import_many_books_and_repeated_progress_highlight_writes_recover(self):
        """Exercise ordinary library writes, then reopen exactly as after restart."""
        root = Path(self._tmp.name) / "books"
        root.mkdir()
        library = Library(self.data_dir)
        entries = []
        started = time.perf_counter()
        for i in range(30):
            path = root / f"synthetic-{i:02d}.epub"
            make_epub(path, [(f"第 {c + 1} 章", [LONG] * 12) for c in range(4)])
            entries.append(library.add(path))
        import_s = time.perf_counter() - started

        started = time.perf_counter()
        writes = 0
        for i in range(600):
            entry = entries[i % len(entries)]
            library.save_position(entry.id, Loc(i % 4, (i * 7) % 14))
            writes += 1
        for i in range(120):
            entry = entries[i % len(entries)]
            library.save_highlights(entry.id, [{"doc": i % 4, "block": i % 14,
                                                "start": 0, "end": 8,
                                                "text": f"stress-{i}"}])
            writes += 1
        write_s = time.perf_counter() - started

        restarted = Library(self.data_dir)
        self.assertEqual(len(restarted.books()), 30)
        self.assertEqual(restarted.get(entries[599 % 30].id).position,
                         Loc(599 % 4, (599 * 7) % 14))
        self.assertEqual(restarted.highlights(entries[119 % 30].id)[0]["text"], "stress-119")
        self.assertEqual(writes, 720)
        # Bound gross regressions while keeping CI machines of varying speed usable.
        self.assertLess(import_s, 30)
        self.assertLess(write_s, 30)
        print(f"Stress metrics: imported 30 synthetic EPUBs in {import_s:.3f}s; "
              f"replayed {writes} progress/highlight writes in {write_s:.3f}s")

    def test_epub_view_turn_zoom_highlight_churn(self):
        """Repeatedly navigate and reflow a long synthetic chapter in Qt offscreen."""
        path = Path(self._tmp.name) / "long.epub"
        paras = [LONG] * 180
        make_epub(path, [("长章", paras)])
        book = open_book(path)
        self.addCleanup(book.zip.close)
        view = EpubView()
        self.addCleanup(view.deleteLater)
        view.resize(940, 760)
        view.open(book, None, [])
        deadline = time.monotonic() + 12
        while not view.layout_done() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        self.assertTrue(view.layout_done(), "initial long EPUB layout timed out")
        rss_before = _working_set_mb()

        started = time.perf_counter()
        turns = 0
        for i in range(500):
            view.page_forward()
            turns += 1
            if i % 7 == 0:
                view.page_back()
                turns += 1
        turn_s = time.perf_counter() - started

        # Font range has 6 steps; alternate to force real re-layouts.
        started = time.perf_counter()
        for i in range(24):
            view.zoom(1 if i % 2 == 0 else -1)
        zoom_s = time.perf_counter() - started

        started = time.perf_counter()
        doc = view._ensure_paged(0).doc
        highlights = 0
        for i in range(100):
            block = doc.findBlockByNumber(1 + i)
            a = block.position()
            made = view.add_highlight(0, a, a + min(18, block.length() - 1))
            highlights += bool(made)
        highlight_s = time.perf_counter() - started
        self.assertEqual(highlights, 100)
        self.assertEqual(len(view.highlights()), 100)
        self.assertGreater(turns, 500)
        self.assertLess(turn_s, 20)
        self.assertLess(zoom_s, 20)
        self.assertLess(highlight_s, 20)
        rss_after = _working_set_mb()
        if rss_before is not None and rss_after is not None:
            self.assertLess(rss_after - rss_before, 256,
                            f"unexpected working-set growth {rss_before:.1f} → {rss_after:.1f} MiB")
        print(f"Stress metrics: {turns} turn calls in {turn_s:.3f}s; 24 zoom calls in {zoom_s:.3f}s; "
              f"100 highlights in {highlight_s:.3f}s; working set "
              f"{rss_before:.1f}→{rss_after:.1f} MiB" if rss_before is not None and rss_after is not None else
              f"Stress metrics: {turns} turn calls in {turn_s:.3f}s; 24 zoom calls in {zoom_s:.3f}s; "
              f"100 highlights in {highlight_s:.3f}s")

    def test_pdf_renderer_processes_are_reaped_after_rapid_close(self):
        """Open/render/close PDFs repeatedly and verify each child exits."""
        path = make_pdf(Path(self._tmp.name) / "render-stress.pdf", 24)
        started = time.perf_counter()
        pids = []
        with mock.patch.dict(os.environ, {"READER_PDF_RENDER": ""}):
            for _ in range(4):
                book = open_book(path)
                view = PdfView()
                view.resize(1000, 760)
                view.show()
                view.open(book, None, [])
                renderer = view._renderer
                self.assertIsNotNone(renderer)
                self.assertEqual(renderer.mode, "process", "PDF renderer fell back to a thread")
                child = renderer._proc
                pids.append(child.pid)
                # Let the pipe/pump and first visible-page request become active.
                end = time.monotonic() + 1.5
                while time.monotonic() < end and child.is_alive():
                    app.processEvents()
                    if renderer._last:
                        break
                    time.sleep(0.002)
                view.close()
                renderer.close()  # idempotence during close/destruction
                child.join(timeout=2)
                self.assertFalse(child.is_alive(), f"PDF renderer child {child.pid} leaked")
                renderer._pump.join(timeout=2)
                self.assertFalse(renderer._pump.is_alive(), "PDF renderer response thread leaked")
                book.doc.close()
                view.deleteLater()
                app.processEvents()
        elapsed = time.perf_counter() - started
        self.assertEqual(len(set(pids)), 4)
        self.assertLess(elapsed, 30)
        print(f"Stress metrics: started and reaped 4 PDF render processes in {elapsed:.3f}s")

    def test_rapid_reading_session_start_end_cycles(self):
        """Warm then batch real session teardown through ReaderApp.deleteLater."""
        theme.set_theme("dark", save=False)
        class Window(QObject):
            start_requested = Signal(object, int)

            def bring_to_front(self):
                pass

            def show_session(self, *_args):
                pass

        window = Window()
        rows = []
        destroyed = {"screen": 0, "widget": 0}
        refs = {"screen": [], "widget": []}

        def theme_receivers():
            return theme.bus().receivers("2changed(QString)")
        start_rss = _working_set_mb()

        def clean_turn(index):
            root = Path(self._tmp.name) / f"session-{index}"
            root.mkdir()
            rig = Rig(root, logged_in=False, locked=False)
            # The actual ReaderApp only needs these two persistence calls to
            # finish; this fixture is focused on Qt object teardown.
            rig.lib.place = lambda *_args: {"section": ""}
            rig.lib.log_session = lambda *_args, **_kwargs: None
            ctl = ReaderApp(window, rig.lib)
            # ReaderApp.begin normally installs these fields before start().
            ctl.screen, ctl.entry, ctl.started = rig.rs, rig.entry, datetime.now()
            rig.rs.finished.connect(ctl._finished)
            refs["screen"].append(weakref.ref(rig.rs))
            refs["widget"].append(weakref.ref(rig.rs.widget))
            rig.rs.destroyed.connect(lambda *_: destroyed.__setitem__("screen", destroyed["screen"] + 1))
            rig.rs.widget.destroyed.connect(lambda *_: destroyed.__setitem__("widget", destroyed["widget"] + 1))
            rig.start()
            for _ in range(3):
                rig.view.page_forward()
            rig.rs.finish()
            self.assertEqual(len(rig.summaries), 1)
            self.assertEqual(rig.summaries[0]["reason"], "done")
            self.assertIsNone(ctl.screen)
            # Match the app cleanup path: _finished schedules deleteLater.
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            if rig.rs.book is not None and hasattr(rig.rs.book, "zip"):
                rig.rs.book.zip.close()
            del ctl, rig

        started = time.perf_counter()
        # A short warm-up absorbs font/layout initialization before measuring trend.
        for i in range(3):
            clean_turn(i)
        gc.collect()
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        baseline = _working_set_mb()
        baseline_toplevels = len(app.topLevelWidgets())
        rows.append((3, baseline, baseline_toplevels,
                     len(multiprocessing.active_children()),
                     sum(t.is_alive() for t in threading.enumerate()
                         if t.name.startswith("reader-pdf-")),
                     theme_receivers(),
                     sum(ref() is not None for ref in refs["screen"]),
                     sum(ref() is not None for ref in refs["widget"])))

        completed = 3
        for batch in range(1, 12):
            for _ in range(5):
                clean_turn(completed)
                completed += 1
            gc.collect()
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            rss = _working_set_mb()
            row = (completed, rss, len(app.topLevelWidgets()),
                   len(multiprocessing.active_children()),
                   sum(t.is_alive() for t in threading.enumerate()
                       if t.name.startswith("reader-pdf-")),
                   theme_receivers(),
                   sum(ref() is not None for ref in refs["screen"]),
                   sum(ref() is not None for ref in refs["widget"]))
            rows.append(row)
            if rss is not None and rss > 400:
                break
        # If the first 38 cycles stayed below the safety ceiling, advance one
        # cycle at a time near it so a leak cannot overshoot by a full batch.
        while completed < 60 and (rows[-1][1] is None or rows[-1][1] < 394):
            clean_turn(completed)
            completed += 1
            gc.collect()
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            rss = _working_set_mb()
            row = (completed, rss, len(app.topLevelWidgets()),
                   len(multiprocessing.active_children()),
                   sum(t.is_alive() for t in threading.enumerate()
                       if t.name.startswith("reader-pdf-")),
                   theme_receivers(),
                   sum(ref() is not None for ref in refs["screen"]),
                   sum(ref() is not None for ref in refs["widget"]))
            rows.append(row)
            if rss is not None and rss >= 394:
                break

        elapsed = time.perf_counter() - started
        print("Session-cycle metrics (cycles, MiB, top-level widgets, child processes, PDF threads, "
              f"theme receivers, live screen/widget wrappers): {rows}")
        print(f"Session object destruction callbacks: {destroyed}")
        print(f"Stress metrics: completed {completed} ReaderApp session teardown cycles in {elapsed:.3f}s; "
              f"working set {start_rss:.1f}→{rows[-1][1]:.1f} MiB" if start_rss is not None and rows[-1][1] is not None else
              f"Stress metrics: completed {completed} ReaderApp session teardown cycles in {elapsed:.3f}s")
        self.assertGreaterEqual(completed, 39)
        self.assertLess(elapsed, 90)
        self.assertEqual(destroyed["screen"], completed, destroyed)
        self.assertEqual(destroyed["widget"], completed, destroyed)
        self.assertTrue(all(row[2] == baseline_toplevels for row in rows), rows)
        self.assertTrue(all(row[3] == 0 and row[4] == 0 for row in rows), rows)
        self.assertTrue(all(row[5] == rows[0][5] for row in rows), rows)
        self.assertTrue(all(row[6] == 0 and row[7] == 0 for row in rows), rows)
        if rows[-1][1] is not None:
            self.assertLessEqual(rows[-1][1], 400, f"resource probe stopped at 400 MiB: {rows}")


if __name__ == "__main__":
    unittest.main()
