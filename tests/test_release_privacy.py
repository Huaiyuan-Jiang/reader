"""Release regressions: no legacy key imports or echoed server secrets."""
import ast
import unittest
from pathlib import Path

from reader.ai import mimo


class ReleasePrivacyTests(unittest.TestCase):
    def test_no_legacy_credential_imports(self):
        root = Path(__file__).resolve().parents[1] / 'reader'
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn('_mimo_key', [a.name for a in node.names], str(path))

    def test_server_error_does_not_echo_response_body(self):
        self.assertNotIn('private-fixture', mimo._friendly(400, 'private-fixture'))
