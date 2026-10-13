"""验证默认两步界面、隐藏高级参数和可复制接入提示词。"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('READER_NO_LOCK', '1')
import tempfile
import unittest
from unittest import mock
from PySide6.QtWidgets import QApplication
from reader.ai import config
from reader.ai.claude import Result
from reader.ui import theme
from reader.ui.ai_settings import AISettingsDialog

app = QApplication.instance() or QApplication([])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'READER_DATA_DIR': self.tmp.name, 'READER_AI_CONFIG': '',
                                'READER_AI': '', 'READER_MODEL': '', 'READER_BASE_URL': ''})
        patch.start()
        self.addCleanup(patch.stop)
        self.dialog = AISettingsDialog()
        self.addCleanup(self.dialog.close)

    def select(self, pid):
        self.dialog.choice.setCurrentIndex(self.dialog.choice.findData(pid))

    def test_default_only_provider_key_and_connect(self):
        self.select('deepseek')
        d = self.dialog
        self.assertTrue(d.form.isRowVisible(d.key))
        for k in ('model', 'base_url', 'api_key_env', 'auth_header'):
            self.assertFalse(d.form.isRowVisible(d.fields[k]))
        self.assertTrue(d.check_btn.isHidden())
        self.assertTrue(d.test_btn.isHidden())
        self.assertEqual(d.save_btn.text(), '连接并使用')
        d._toggle_advanced()
        self.assertTrue(d.form.isRowVisible(d.fields['model']))
        self.assertTrue(d.form.isRowVisible(d.fields['base_url']))

    def test_api_connect_saves_and_tests_without_exposing_advanced_fields(self):
        self.select('deepseek')
        self.dialog.key.setText('test-key')
        def immediately(fn, callback):
            callback(fn(), None)
        with mock.patch('reader.ui.ai_settings.background', immediately), \
             mock.patch('reader.ai.secrets.put') as save_key, \
             mock.patch('reader.ai.provider.ask_once', return_value=Result(text='OK')):
            self.dialog._quick_connect()
        save_key.assert_called_once_with('deepseek', 'test-key')
        self.assertEqual(config.load()['active'], 'deepseek')
        self.assertEqual(self.dialog.result(), AISettingsDialog.Accepted)

    def test_local_harness_copies_dynamic_prompt_without_api_key(self):
        from PySide6.QtGui import QGuiApplication
        self.select('harness')
        self.assertFalse(self.dialog.form.isRowVisible(self.dialog.key))
        self.assertEqual(self.dialog.save_btn.text(), '复制接入提示词')
        self.dialog._quick_connect()
        text = QGuiApplication.clipboard().text()
        self.assertIn('reader', text)
        self.assertIn('docs/HARNESS.md', text)
        self.assertIn('harness_command', text)
        self.assertFalse(config.config_path().exists())

    def test_claude_hides_api_key(self):
        self.select('claude')
        self.assertFalse(self.dialog.form.isRowVisible(self.dialog.key))
        self.assertTrue(self.dialog.form.isRowVisible(self.dialog.code_help))


if __name__ == '__main__':
    unittest.main()
