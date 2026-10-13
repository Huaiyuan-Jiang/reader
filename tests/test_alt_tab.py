import os
import unittest
from unittest.mock import patch, Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from reader.lock.shortcuts import AltTabGuard


class AltTabTests(unittest.TestCase):
    def test_forward_and_reverse_switch_swallowed_in_reader_only(self):
        guard = AltTabGuard()
        for down, up in ((0x104, 0x105), (0x100, 0x101)):
            self.assertTrue(guard.should_block(down, 9, 0x20, True))
            self.assertTrue(guard.should_block(up, 9, 0, False))
        self.assertFalse(guard.should_block(0x104, 9, 0x20, False))
        self.assertFalse(guard.should_block(0x100, 9, 0, True))
        self.assertFalse(guard.should_block(0x104, 0x1b, 0x20, True))

    def test_test_mode_never_installs_hook(self):
        with patch.dict(os.environ, {'READER_NO_LOCK': '1'}):
            self.assertFalse(AltTabGuard().start())

    def test_native_release_is_idempotent(self):
        guard = AltTabGuard()
        guard._user = Mock()
        guard._hook = 123
        guard._callback = object()
        guard.stop()
        guard.stop()
        guard._user.UnhookWindowsHookEx.assert_called_once_with(123)
        self.assertIsNone(guard._callback)

    def test_time_up_and_window_close_release_guard(self):
        from PySide6.QtWidgets import QApplication
        from reader.lock.windows import ReadingWindow
        from reader.ui.reading import release_on_top
        app = QApplication.instance() or QApplication([])
        window = ReadingWindow()
        window._shortcuts = Mock()
        release_on_top(window)
        window._shortcuts.stop.assert_called_once()
        window._shortcuts.stop.reset_mock()
        window.close()
        window._shortcuts.stop.assert_called_once()
        window.deleteLater()
        app.processEvents()
