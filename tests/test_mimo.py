"""MiMo 接口（假的 HTTP 回包，不花额度）。运行：python -m unittest tests.test_mimo"""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
import urllib.error
from unittest import mock

from reader.ai import mimo, provider
from reader.ai.claude import Delta, Result


def _sse(pieces: list[str], usage=None) -> bytes:
    lines = []
    for p in pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": p}, "index": 0}]}, ensure_ascii=False))
    if usage:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n").encode("utf-8")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class MimoTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        patcher = mock.patch.dict(os.environ, {"READER_MIMO_KEY": "sk-test", "READER_AI": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake(self, *replies):
        replies = list(replies)

        def urlopen(req, timeout=None):
            self.sent.append((dict(req.header_items()), json.loads(req.data.decode("utf-8"))))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return _Resp(r)
        return mock.patch("urllib.request.urlopen", urlopen)

    def test_stream_and_multi_turn(self):
        with self._fake(_sse(["你", "好"], {"prompt_tokens": 12, "completion_tokens": 2}), _sse(["记得"])):
            evs = list(mimo.run("第一问", system="系统提示"))
            self.assertEqual([e.text for e in evs if isinstance(e, Delta)], ["你", "好"])
            res = evs[-1]
            self.assertTrue(res.ok)
            self.assertEqual((res.text, res.tokens_in, res.tokens_out), ("你好", 12, 2))
            evs2 = list(mimo.run("第二问", resume=res.session_id))
        headers, body = self.sent[0]
        self.assertEqual(headers.get("Api-key"), "sk-test")          # key 在 api-key 头里
        self.assertEqual(body["model"], "mimo-v2.6-pro")
        self.assertEqual(body["thinking"], {"type": "disabled"})     # 问答不开思考
        _, body2 = self.sent[1]
        self.assertEqual([m["role"] for m in body2["messages"]], ["system", "user", "assistant", "user"])
        self.assertEqual(body2["messages"][2]["content"], "你好")
        self.assertEqual(evs2[-1].session_id, res.session_id)

    def test_lost_session_asks_companion_to_restart(self):
        res = list(mimo.run("问", resume="没有这个"))[-1]
        self.assertIn("resume", res.error)

    def test_http_errors_are_readable(self):
        err = urllib.error.HTTPError(mimo.URL, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))
        with self._fake(err):
            res = list(mimo.run("问"))[-1]
        self.assertIn("key", res.error)
        with self._fake(urllib.error.URLError("no route")):
            res = list(mimo.run("问"))[-1]
        self.assertIn("连不上", res.error)

    def test_stop(self):
        stop = threading.Event()
        stop.set()
        with self._fake(_sse(["a", "b"])):
            res = list(mimo.run("问", stop=stop))[-1]
        self.assertEqual(res.error, "已停止")

    def test_ask_once_thinks_and_forgets(self):
        with self._fake(_sse(['{"a":1}'])):
            res = mimo.ask_once("出题", system="出题的规矩")
        self.assertTrue(res.ok)
        self.assertIsNone(res.session_id)
        self.assertNotIn("thinking", self.sent[0][1])               # 出题开着思考（默认）

    def test_image_goes_as_data_url(self):
        with self._fake(_sse(["图"])):
            list(mimo.run("这页是什么", images=[b"\x89PNG..."]))
        content = self.sent[0][1]["messages"][-1]["content"]
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_provider_choice(self):
        self.assertEqual(provider.name(), "mimo")
        self.assertFalse(provider.needs_login())
        with mock.patch.dict(os.environ, {"READER_AI": "claude"}):
            self.assertEqual(provider.name(), "claude")
        with mock.patch.object(mimo, "api_key", return_value=""):
            self.assertEqual(provider.name(), "claude")                 # 没有 key：退回 Claude


if __name__ == "__main__":
    unittest.main()
