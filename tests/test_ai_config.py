"""验证配置边界、凭据隔离与环境变量覆盖。不访问网络。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from reader.ai import config, secrets, provider


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {'READER_DATA_DIR': self.tmp.name, 'READER_AI_CONFIG': '',
            'READER_AI': '', 'READER_MODEL': '', 'READER_BASE_URL': '', 'CLAUDE_CLI': '',
            'READER_CLAUDE_CONFIG_DIR': '', 'OPENAI_API_KEY': '', 'DEEPSEEK_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_default_preserves_old_choice_and_explicit_unknown_fails(self):
        with mock.patch('reader.ai.mimo.available', return_value=True):
            self.assertEqual(provider.name(), 'mimo')
        with mock.patch('reader.ai.mimo.available', return_value=False):
            self.assertEqual(provider.name(), 'claude')
        with mock.patch.dict(os.environ, {'READER_AI': 'missing'}):
            self.assertFalse(provider.logged_in())
            self.assertIn('找不到', provider.setup_error())
            self.assertFalse(list(provider.run('问'))[-1].ok)

    def test_roundtrip_and_env_override(self):
        data = config.defaults()
        data['active'] = 'deepseek'
        data['profiles'][2]['model'] = 'my-next-model'
        config.save(data)
        self.assertEqual(provider.name(), 'deepseek')
        self.assertEqual(provider.profile().model, 'my-next-model')
        with mock.patch.dict(os.environ, {'READER_AI': 'gpt', 'READER_MODEL': 'my-gpt'}):
            self.assertEqual(provider.profile().model, 'my-gpt')
        saved = json.loads(config.config_path().read_text(encoding='utf-8'))
        self.assertTrue(all('api_key' not in p for p in saved['profiles']))

    def test_corrupted_config_does_not_silently_fallback(self):
        config.config_path().write_text('{bad json', encoding='utf-8')
        self.assertIn('无法读取', provider.setup_error())
        self.assertFalse(list(provider.run('问'))[-1].ok)

    def test_missing_profile_and_reserved_extra_parameters_rejected(self):
        data = config.defaults()
        data['active'] = 'absent'
        with self.assertRaises(config.ConfigError):
            config.save(data)
        p = config.Profile('custom', '本机', 'chat_completions', 'model', 'http://localhost:123/v1')
        p.extra_body = {'messages': []}
        with self.assertRaises(config.ConfigError):
            p.validate()

    def test_no_credentials_or_remote_http_in_url(self):
        for url in ('http://example.com/v1', 'https://user:key@example.com/v1',
                    'https://example.com/v1?api_key=secret'):
            p = config.Profile('custom', 'custom', 'responses', 'm', url)
            with self.assertRaises(config.ConfigError):
                p.validate()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_encrypted_key_roundtrip_and_deletion(self):
        secrets.put('deepseek', 'a-test-secret-value')
        text = (Path(self.tmp.name) / 'ai-secrets.json').read_text(encoding='utf-8')
        self.assertNotIn('a-test-secret-value', text)
        self.assertEqual(secrets.get('deepseek'), 'a-test-secret-value')
        secrets.put('deepseek', '')
        self.assertEqual(secrets.get('deepseek'), '')

    def test_code_directory_uses_selected_profile(self):
        from reader.ai import claude
        data = config.defaults()
        data['active'] = 'claude'
        data['profiles'][0]['claude_config_dir'] = str(Path(self.tmp.name) / 'account')
        config.save(data)
        self.assertEqual(claude.child_env()['CLAUDE_CONFIG_DIR'], str(Path(self.tmp.name) / 'account'))
        self.assertEqual(claude.work_dir(), Path(self.tmp.name) / 'claude-cwd')


if __name__ == '__main__':
    unittest.main()
