"""Adversarial persistence input regression checks using only temporary files."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf
from PySide6.QtWidgets import QApplication

from reader.library import Library
from tests.test_books_ai import LONG, make_epub
from reader.ui import theme

_app = QApplication.instance() or QApplication([])


class RecoveryInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="reader-adversarial-"))
        self.old_data = os.environ.get("READER_DATA_DIR")
        os.environ["READER_DATA_DIR"] = str(self.tmp / "user-data")
        theme.set_theme("dark", save=False)

    def tearDown(self):
        if self.old_data is None:
            os.environ.pop("READER_DATA_DIR", None)
        else:
            os.environ["READER_DATA_DIR"] = self.old_data

    def epub(self, name: str) -> Path:
        return make_epub(self.tmp / name, [("第一章", [LONG])])

    def test_bom_library_json_preserves_books_and_shows_recovery_warning(self):
        """A BOM is valid JSON input and must not make a later add discard existing entries."""
        data = self.tmp / "data"
        lib = Library(data)
        first = lib.add(self.epub("one.epub"))
        second = lib.add(self.epub("two.epub"))
        original = (data / "library.json").read_bytes()
        (data / "library.json").write_bytes(b"\xef\xbb\xbf" + original)

        reopened = Library(data)
        self.assertEqual({b.id for b in reopened.books()}, {first.id, second.id})
        reopened.add(self.epub("three.epub"))
        self.assertEqual(len(Library(data).books()), 3)
        self.assertEqual(list(data.glob("library.json.recovery-*.bak")), [])

    def test_partial_corrupt_library_keeps_valid_entry_backup_and_visible_warning(self):
        data = self.tmp / "data"
        lib = Library(data)
        entry = lib.add(self.epub("kept.epub"))
        raw = json.loads((data / "library.json").read_text("utf-8"))
        raw["books"]["bad-entry"] = {"id": "bad-entry", "path": "x.epub", "title": "broken",
                                       "kind": "epub", "position": [1, "2"]}
        original = json.dumps(raw, ensure_ascii=False).encode("utf-8")
        (data / "library.json").write_bytes(original)

        recovered = Library(data)
        self.assertIsNotNone(recovered.get(entry.id))
        self.assertEqual(len(recovered.books()), 1)
        backups = list(data.glob("library.json.recovery-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        # Recovery must leave source untouched until an intentional later save.
        self.assertEqual((data / "library.json").read_bytes(), original)

        from reader.ui.shelf import Notice, ShelfWindow

        status = self.tmp / "status.json"
        status.write_text('{"days":{}}', encoding="utf-8")
        class NoOcr:
            def state(self, _bid): return None
            def busy(self): return False
            def enqueue(self, _entry): pass
            def resume_all(self): pass
            def stop(self, timeout=0): pass
        win = ShelfWindow(recovered, status_path=status, check_login=False, ocr=NoOcr())
        win.show()
        _app.processEvents()
        notices = win.findChildren(Notice)
        self.assertTrue(any(n.isVisible() and "书架文件有损坏" in n.text.text() for n in notices))
        win.close()
        recovered.set_setting("recovery_test", "saved")
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertIsNotNone(Library(data).get(entry.id))

    def test_invalid_extra_doc_sizes_do_not_crash_progress_or_destroy_entry(self):
        data = self.tmp / "data"
        lib = Library(data)
        entry = lib.add(self.epub("extra.epub"))
        lib.save_position(entry.id, __import__("reader.books", fromlist=["Loc"]).Loc(0, 1))
        extra = data / "books" / f"{entry.id}.json"
        original = extra.read_bytes()
        extra.write_text('{"doc_sizes":"999999999999999999999999999"}', encoding="utf-8")

        # Invalid cache/extra metadata should degrade to unknown progress, not crash.
        self.assertIsNone(lib.progress(entry.id))
        self.assertIsNotNone(lib.get(entry.id))
        self.assertTrue(extra.exists())
        self.assertNotEqual(extra.read_bytes(), original)

    def test_malformed_and_bom_ui_json_can_still_be_read_and_saved(self):
        from reader.ui import theme

        settings = Path(os.environ["READER_DATA_DIR"]) / "ui.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        for invalid in (b"null", b"[]", b"5", b'"dark"', b"\xef\xbb\xbf{\"theme\":\"dark\"}"):
            settings.write_bytes(invalid)
            self.assertIn(theme.saved_choice(), ("dark", "light", "system"))
            theme.set_theme("light")
            data = json.loads(settings.read_text("utf-8-sig"))
            self.assertEqual(data.get("theme"), "light")

    def test_ocr_cache_with_only_out_of_range_pages_is_not_marked_complete(self):
        from reader.books import ocr

        pdf = self.tmp / "scan.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        cache = ocr.ocr_dir() / f"{ocr.file_key(pdf)}.json"
        cache.write_text(json.dumps({"lang": "en-US", "pages": {
            "100": {"text": "not a real page", "words": []},
            "101": {"text": "also not a real page", "words": []}}}), encoding="utf-8")

        with mock.patch.object(ocr, "available", return_value=True):
            entry = Library(self.tmp / "data").add(pdf)
        self.assertFalse(entry.ocr_done)
        self.assertEqual(entry.pages, 2)


if __name__ == "__main__":
    unittest.main()
