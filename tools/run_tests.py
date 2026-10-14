"""Run GUI test modules in separate processes and fresh temporary data folders."""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
failed = []
with tempfile.TemporaryDirectory(prefix='reader-tests-') as temporary:
    for path in sorted((root / 'tests').glob('test_*.py')):
        env = os.environ.copy()
        env.update(QT_QPA_PLATFORM='offscreen', READER_NO_LOCK='1', PYTHONUTF8='1',
                   READER_DATA_DIR=str(Path(temporary) / path.stem))
        process = subprocess.Popen([sys.executable, '-m', 'unittest', 'tests.' + path.stem], cwd=root, env=env)
        try:
            code = process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], check=False)
            process.wait()
            code = 124
        if code:
            failed.append(path.stem)
print('Failed modules:', failed)
raise SystemExit(1 if failed else 0)
