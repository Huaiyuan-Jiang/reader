"""Tutorial entry and theme subscription lifetime regressions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('READER_NO_LOCK', '1')
from PySide6.QtCore import QCoreApplication, QEvent, SIGNAL
from PySide6.QtWidgets import QApplication, QWidget
from reader.library import Library
from reader.ui import theme
from reader.ui.shelf import ShelfWindow


class TutorialAndLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory(prefix='reader-guide-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {'READER_DATA_DIR': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_guide_opens_packaged_local_file_without_network_or_login(self):
        window = ShelfWindow(Library(self.root), status_path=self.root / 'status.json', check_login=False)
        try:
            with mock.patch('reader.ui.shelf.QDesktopServices.openUrl', return_value=True) as opened:
                self.assertTrue(window.open_guide())
                url = opened.call_args.args[0]
                self.assertTrue(url.isLocalFile())
                self.assertEqual(Path(url.toLocalFile()).name, 'guide.html')
                self.assertTrue(Path(url.toLocalFile()).is_file())
                self.assertEqual(window.guide_btn.text(), '新手教程')
        finally:
            window.ocr.stop()
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_destroyed_widgets_release_plain_and_bound_theme_callbacks(self):
        calls = []
        class Widget(QWidget):
            def changed(self, name):
                calls.append(name)
        bus = theme.bus()
        before = bus.receivers(SIGNAL('changed(QString)'))
        widgets = [Widget() for _ in range(20)]
        for widget in widgets:
            theme.on_change(widget.changed)
            theme.style(widget, 'QWidget {{ color: {t1}; }}')
        bus.changed.emit(theme.current())
        self.assertEqual(len(calls), 20)
        for widget in widgets:
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(bus.receivers(SIGNAL('changed(QString)')), before)
        bus.changed.emit(theme.current())
        self.assertEqual(len(calls), 20)
