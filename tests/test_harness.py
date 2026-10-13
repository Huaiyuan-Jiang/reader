"""启动真的本地桥接测试进程，不调用模型、不访问网络。"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from reader.ai import config, harness
from reader.ai.claude import Delta


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'READER_DATA_DIR': self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.script = Path(self.tmp.name) / 'bridge.py'
        self.p = config.Profile('harness', '本地 Harness', 'local_harness', 'local-harness',
                                supports_images=False, harness_command=[sys.executable, str(self.script)])
        harness._sessions.clear()

    def write(self, source):
        self.script.write_text(source, encoding='utf-8')

    def test_real_process_stream_and_history(self):
        self.write("import json,sys\nr=json.loads(sys.stdin.readline())\n"
                   "text=str(len(r['messages']))\n"
                   "print(json.dumps({'type':'delta','text':text}),flush=True)\n"
                   "print(json.dumps({'type':'result','text':text,'tokens_in':7,'tokens_out':1}),flush=True)\n")
        events = list(harness.run('第一问', profile=self.p, system='规则'))
        self.assertEqual([e.text for e in events if isinstance(e, Delta)], ['2'])
        r = events[-1]
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.tokens_in, r.tokens_out), (7, 1))
        r2 = list(harness.run('第二问', profile=self.p, resume=r.session_id))[-1]
        self.assertEqual(r2.text, '4')
        self.assertTrue(r2.ok, r2.error)

    def test_final_only_and_ask_once_cleans_history(self):
        self.write("import json,sys\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'type':'result','text':'OK'}),flush=True)\n")
        r = harness.ask_once('问', profile=self.p, system='规则')
        self.assertTrue(r.ok, r.error)
        self.assertIsNone(r.session_id)
        self.assertFalse(harness._sessions)

    def test_bad_output_and_nonzero_exit_fail(self):
        for source in ("print('Welcome to CLI')\n", "import json\nprint(json.dumps({'type':'result','text':'OK'}))\nraise SystemExit(2)\n"):
            self.write(source)
            r = list(harness.run('问', profile=self.p))[-1]
            self.assertFalse(r.ok)

    def test_stop_and_timeout_terminate_process(self):
        self.write("import sys,time\nsys.stdin.readline()\ntime.sleep(30)\n")
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        t0 = time.monotonic()
        try:
            r = list(harness.run('问', profile=self.p, stop=stop))[-1]
        finally:
            timer.cancel()
        self.assertEqual(r.error, '已停止')
        self.assertLess(time.monotonic() - t0, 4)
        r = list(harness.run('问', profile=self.p, timeout=1))[-1]
        self.assertIn('超时', r.error)

    def test_missing_connection_or_unsupported_image_is_explicit(self):
        self.p.harness_command = []
        self.assertIn('接入提示词', list(harness.run('问', profile=self.p))[-1].error)
        self.p.harness_command = [sys.executable, str(self.script)]
        self.assertIn('图片', list(harness.run('问', profile=self.p, images=[b'png']))[-1].error)


if __name__ == '__main__':
    unittest.main()
