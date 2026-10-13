"""Regression tests for malformed local files and adversarial AI output.

All fixtures are synthetic and every persistent reader file lives in a temporary
directory. These tests do not invoke a real model or Windows OCR.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pymupdf

from reader.ai import config
from reader.ai.companion import parse_quiz
from reader.books import Loc, open_book
from reader.books import ocr
from reader.library import Library


def make_epub(path: Path, *, body: str = "<h1>第一章</h1><p>合成正文。</p>") -> Path:
    """Write a tiny EPUB; body='' produces a package with no readable content."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/>'
            "</rootfiles></container>",
        )
        z.writestr(
            "OEBPS/book.opf",
            '<package><metadata><dc:title>合成测试书</dc:title></metadata><manifest>'
            '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>',
        )
        z.writestr("OEBPS/chapter.xhtml", f"<html><body>{body}</body></html>")
    return path


def make_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Synthetic PDF content")
    doc.save(path)
    doc.close()
    return path


class AdversarialInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reader-adversarial-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "reader-data"
        self.env = mock.patch.dict(
            os.environ,
            {
                "READER_NO_LOCK": "1",
                "QT_QPA_PLATFORM": "offscreen",
                "READER_DATA_DIR": str(self.data),
                "READER_AI_CONFIG": "",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_corrupt_library_is_backed_up_and_reported_before_replacement(self):
        root = self.root / "corrupt-library"
        root.mkdir()
        raw = '{"books": {"old": '
        path = root / "library.json"
        path.write_text(raw, encoding="utf-8")

        lib = Library(root)
        self.assertTrue(lib.load_warnings, "corruption should create a visible shelf warning")
        backups = list(root.glob("library.json.recovery-*.bak"))
        self.assertTrue(backups, "the damaged source must be preserved before recovery")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), raw)

        # A subsequent normal write must not destroy the only copy of the damaged file.
        lib.add(make_epub(self.root / "new.epub"))
        self.assertEqual(backups[0].read_text(encoding="utf-8"), raw)

    def test_bad_library_container_or_entry_is_isolated_while_valid_books_survive(self):
        source = make_epub(self.root / "good.epub")
        root = self.root / "mixed-library"
        lib = Library(root)
        good = lib.add(source)
        payload = json.loads((root / "library.json").read_text(encoding="utf-8"))
        payload["books"]["broken-null-path"] = {
            "id": "broken-null-path",
            "path": None,
            "title": "bad",
            "kind": "epub",
            "position": [0],
        }
        payload["books"]["broken-kind"] = {
            "id": "broken-kind",
            "path": "ghost.epub",
            "title": "bad kind",
            "kind": "unknown",
            "position": None,
        }
        payload["books"]["broken-boolean"] = {
            "id": "broken-boolean",
            "path": "ghost.epub",
            "title": "bad flag",
            "kind": "epub",
            "scanned": "false",
            "ocr_done": 1,
            "position": None,
        }
        payload["books"]["broken-counts"] = {
            "id": "broken-counts",
            "path": "ghost.pdf",
            "title": "bad numeric fields",
            "kind": "pdf",
            "pages": True,
            "position": [False, 0],
            "quiz_done": [True],
        }
        (root / "library.json").write_text(json.dumps(payload), encoding="utf-8")

        recovered = Library(root)
        entries = recovered.books()
        self.assertEqual([entry.id for entry in entries], [good.id])
        self.assertTrue(recovered.load_warnings)
        surviving_book = open_book(entries[0].path)
        try:
            self.assertIn("合成正文", surviving_book.text_between(
                surviving_book.sections[0].start, surviving_book.sections[0].end))
        finally:
            surviving_book.zip.close()

        # A malformed top-level container is recoverable too, rather than crashing .books().
        container_root = self.root / "bad-container"
        container_root.mkdir()
        (container_root / "library.json").write_text(
            json.dumps({"version": 1, "books": [], "removed": {}, "current": None}),
            encoding="utf-8",
        )
        container = Library(container_root)
        self.assertEqual(container.books(), [])
        self.assertTrue(container.load_warnings)

        current_root = self.root / "bad-current"
        current_lib = Library(current_root)
        current_book = current_lib.add(make_epub(self.root / "survivor.epub"))
        current_data = json.loads((current_root / "library.json").read_text(encoding="utf-8"))
        current_data["current"] = []
        (current_root / "library.json").write_text(json.dumps(current_data), encoding="utf-8")
        recovered_current = Library(current_root)
        self.assertEqual([entry.id for entry in recovered_current.books()], [current_book.id])
        self.assertIsNone(recovered_current.current())
        self.assertTrue(recovered_current.load_warnings)

    def test_extreme_but_integer_reading_positions_keep_progress_in_range(self):
        lib = Library(self.root / "extreme-position-data")
        pdf_entry = lib.add(make_pdf(self.root / "position.pdf"))
        lib.save_position(pdf_entry.id, Loc(10 ** 1000, 0))
        pdf_progress = lib.progress(pdf_entry.id)
        self.assertIsNotNone(pdf_progress)
        self.assertGreaterEqual(pdf_progress, 0)
        self.assertLessEqual(pdf_progress, 1)

        epub_entry = lib.add(make_epub(self.root / "position.epub"))
        lib.save_position(epub_entry.id, Loc(0, 10 ** 1000))
        epub_progress = lib.progress(epub_entry.id)
        self.assertIsNotNone(epub_progress)
        self.assertGreaterEqual(epub_progress, 0)
        self.assertLessEqual(epub_progress, 1)

    def test_recovery_warning_is_rendered_in_shelf_window(self):
        from PySide6.QtWidgets import QApplication

        from reader.ui.shelf import ShelfWindow

        app = QApplication.instance() or QApplication([])
        root = self.root / "visible-warning"
        root.mkdir()
        (root / "library.json").write_text('{"books": ', encoding="utf-8")
        lib = Library(root)
        window = ShelfWindow(lib, status_path=root / "status.json", check_login=False)
        try:
            notices = [window.notices.itemAt(i).widget().text.text()
                       for i in range(window.notices.count())]
            self.assertTrue(any("书架文件有损坏" in text for text in notices), notices)
        finally:
            window.ocr.stop()
            window.close()
            window.deleteLater()
            app.processEvents()

    def test_truthy_non_path_last_add_directory_is_recovered(self):
        from PySide6.QtWidgets import QApplication

        from reader.ui.shelf import ShelfWindow

        app = QApplication.instance() or QApplication([])
        root = self.root / "bad-settings"
        root.mkdir()
        payload = {"version": 1, "books": {}, "removed": {}, "current": None,
                   "sessions": [], "settings": {"last_add_dir": ["not", "a", "path"]}}
        (root / "library.json").write_text(json.dumps(payload), encoding="utf-8")
        lib = Library(root)
        self.assertTrue(lib.load_warnings)
        window = ShelfWindow(lib, status_path=root / "status.json", check_login=False)
        try:
            self.assertTrue(Path(window._last_dir()).is_dir())
        finally:
            window.ocr.stop()
            window.close()
            window.deleteLater()
            app.processEvents()

    def test_relink_rejects_wrong_or_unopenable_files_without_changing_missing_entry(self):
        original = make_epub(self.root / "original.epub")
        lib = Library(self.root / "relink-data")
        entry = lib.add(original)
        original_path = entry.path
        moved = self.root / "moved-away.epub"
        original.rename(moved)
        self.assertTrue(lib.get(entry.id).missing)

        wrong_extension = self.root / "wrong.txt"
        wrong_extension.write_text("not an ebook", encoding="utf-8")
        malformed_epub = self.root / "broken.epub"
        malformed_epub.write_text("not a zip", encoding="utf-8")
        wrong_format = make_pdf(self.root / "wrong-format.pdf")
        for target in (wrong_extension, malformed_epub, wrong_format):
            with self.subTest(target=target.name):
                with self.assertRaises(ValueError):
                    lib.relink(entry.id, target)
                after = lib.get(entry.id)
                self.assertEqual(after.path, original_path)
                self.assertTrue(after.missing)

        accepted = lib.relink(entry.id, moved)
        self.assertEqual(accepted.path, str(moved.resolve()))
        self.assertFalse(accepted.missing)
        self.assertEqual(accepted.kind, "epub")

    def test_epub_with_no_readable_spine_content_is_rejected(self):
        path = make_epub(self.root / "empty.epub", body="")
        book = None
        try:
            with self.assertRaises(ValueError):
                book = open_book(path)
        finally:
            # Keep the fixture removable on Windows if the buggy parser accepts it.
            if book is not None and hasattr(book, "zip"):
                book.zip.close()

    def test_ocr_cache_with_wrong_json_types_is_ignored_without_crashing(self):
        pdf = make_pdf(self.root / "scan.pdf")
        store = ocr.OcrStore(pdf)
        store.path.write_text(json.dumps({"lang": None, "pages": []}), encoding="utf-8")
        self.assertEqual(store.done_pages(), 0)
        self.assertIsNone(store.page(0))

    def test_ocr_cache_with_malformed_word_records_cannot_crash_pdf_selection(self):
        from unittest import mock

        from reader.books.pdf import PdfBook

        pdf = self.root / "bad-words.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        store = ocr.OcrStore(pdf)
        store.path.write_text(
            json.dumps({"lang": "en-US", "pages": {"0": {
                "text": "cached OCR text", "words": [None, [1, 2], ["x", 2, 3, 4, 5, 6]]
            }}}),
            encoding="utf-8",
        )
        with mock.patch("reader.books.ocr.available", return_value=True):
            book = PdfBook(pdf)
        try:
            self.assertEqual(book.page_text(0), "cached OCR text")
            self.assertEqual(book.page_words(0), [])
        finally:
            book.doc.close()

    def test_ocr_cache_with_unrepresentably_large_json_coordinate_is_ignored(self):
        pdf = self.root / "huge-coordinate.pdf"
        doc = pymupdf.open()
        doc.new_page()
        doc.save(pdf)
        doc.close()
        store = ocr.OcrStore(pdf)
        store.path.write_text(
            json.dumps({"lang": "en-US", "pages": {"0": {
                "text": "cached OCR text", "words": [[10 ** 1000, 0, 1, 1, "word", 0]]
            }}}),
            encoding="utf-8",
        )
        # Constructing a new store exercises startup cache loading, as opening a scanned PDF does.
        loaded = ocr.OcrStore(pdf)
        self.assertEqual(loaded.done_pages(), 1)
        self.assertEqual(loaded.page(0), {"text": "cached OCR text", "words": []})

        store.path.write_text(json.dumps({"lang": "en-US", "pages": None}), encoding="utf-8")
        self.assertEqual(store.done_pages(), 0)
        self.assertIsNone(store.page(0))

    def test_ai_quiz_answer_must_be_a_json_integer_in_range(self):
        def output(answer):
            return json.dumps(
                {
                    "question": "题目",
                    "options": ["甲", "乙", "丙", "丁"],
                    "answer": answer,
                    "hints": ["提示"] * 4,
                    "explain": "解释",
                    "quote": "原文",
                },
                ensure_ascii=False,
            )

        self.assertEqual(parse_quiz(output(2), 0).answer, 2)
        for answer in (1.9, -0.5, True, "1", None, -1, 4):
            with self.subTest(answer=answer):
                self.assertIsNone(parse_quiz(output(answer), 0))

    def test_ai_config_rejects_boolean_version_and_null_or_non_object_profile_list(self):
        valid = config.defaults()
        valid["version"] = True  # Python bool compares equal to 1, but JSON true is not version 1.
        with self.assertRaises(config.ConfigError):
            config.validate(valid)

        for profiles in (None, {}, [None]):
            with self.subTest(profiles=profiles):
                data = config.defaults()
                data["profiles"] = profiles
                with self.assertRaises(config.ConfigError):
                    config.validate(data)


if __name__ == "__main__":
    unittest.main()
