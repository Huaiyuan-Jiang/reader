"""实际构造 HTTP 请求和解析流，用假传输验证不同协议与停止行为。"""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx
from reader.ai import api, config
from reader.ai.claude import Delta, Result


def sse(*events):
    return ''.join('data: ' + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + '\n\n'
                   for e in events).encode()


def chat(text='你好', reason='stop'):
    return sse({'choices': [{'delta': {'reasoning_content': '内部思考'}}]},
               {'choices': [{'delta': {'content': text}}]},
               {'choices': [{'delta': {}, 'finish_reason': reason}]},
               {'usage': {'prompt_tokens': 12, 'completion_tokens': 2}, 'choices': []}, '[DONE]')


class APITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {'READER_DATA_DIR': tmp.name, 'TEST_READER_KEY': 'test-key'})
        env.start()
        self.addCleanup(env.stop)
        self.sent = []
        self.p = config.Profile('test', '测试', 'chat_completions', 'model',
                                'https://test.invalid/v1', 'TEST_READER_KEY')
        api._sessions.clear()

    def transport(self, *replies, status=200):
        replies = iter(replies)
        def handle(req):
            self.sent.append((req, json.loads(req.content)))
            return httpx.Response(status, content=next(replies), headers={'Content-Type': 'text/event-stream'})
        real = httpx.AsyncClient
        return mock.patch.object(api.httpx, 'AsyncClient',
                                 side_effect=lambda **kw: real(transport=httpx.MockTransport(handle), **kw))

    def test_chat_stream_usage_reasoning_history_and_key_header(self):
        with self.transport(chat(), chat('第二次')):
            events = list(api.run('问题', profile=self.p, system='规则'))
            res = events[-1]
            self.assertEqual([e.text for e in events if isinstance(e, Delta)], ['你好'])
            self.assertTrue(res.ok, res.error)
            self.assertEqual((res.tokens_in, res.tokens_out), (12, 2))
            res2 = list(api.run('追问', profile=self.p, resume=res.session_id))[-1]
            self.assertTrue(res2.ok, res2.error)
        self.assertEqual(self.sent[0][0].headers['Authorization'], 'Bearer test-key')
        body = self.sent[1][1]
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(body['messages'][2]['reasoning_content'], '内部思考')

    def test_responses_protocol_images_and_usage(self):
        self.p.protocol = 'responses'
        stream = sse({'type': 'response.output_text.delta', 'delta': '回答'},
                     {'type': 'response.completed', 'response': {'status': 'completed',
                      'usage': {'input_tokens': 8, 'output_tokens': 3}}})
        with self.transport(stream):
            r = list(api.run('看图', profile=self.p, system='规则', images=[b'png']))[-1]
        req, body = self.sent[0]
        self.assertEqual(req.url.path, '/v1/responses')
        self.assertFalse(body['store'])
        self.assertEqual(body['input'][1]['content'][0]['type'], 'input_image')
        self.assertEqual((r.text, r.tokens_in, r.tokens_out), ('回答', 8, 3))
        self.assertTrue(r.ok)

    def test_anthropic_protocol_image_system_and_usage(self):
        self.p.protocol, self.p.auth_header = 'anthropic_messages', 'x-api-key'
        stream = sse({'type': 'message_start', 'message': {'usage': {'input_tokens': 7}}},
                     {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': '回答'}},
                     {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}},
                     {'type': 'message_stop'})
        with self.transport(stream):
            r = list(api.run('看图', profile=self.p, system='规则', images=[b'png']))[-1]
        req, body = self.sent[0]
        self.assertEqual(req.headers['x-api-key'], 'test-key')
        self.assertEqual(req.headers['anthropic-version'], '2023-06-01')
        self.assertEqual(body['system'], '规则')
        self.assertEqual(body['messages'][0]['content'][0]['source']['media_type'], 'image/png')
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.tokens_in, r.tokens_out), (7, 2))

    def test_mimo_auth_and_output_parameter(self):
        self.p.auth_header = 'api-key'
        self.p.token_parameter = 'max_completion_tokens'
        self.p.extra_body = {'thinking': {'type': 'disabled'}}
        with self.transport(chat()):
            r = list(api.run('问', profile=self.p))[-1]
        req, body = self.sent[0]
        self.assertEqual(req.headers['api-key'], 'test-key')
        self.assertEqual(body['max_completion_tokens'], 8192)
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertTrue(r.ok)

    def test_partial_stream_and_truncation_fail(self):
        for reply in (sse({'choices': [{'delta': {'content': '半句'}}]}), chat('半句', 'length'),
                      sse({'type': 'response.incomplete'})):
            with self.transport(reply):
                r = list(api.run('问', profile=self.p))[-1]
            self.assertFalse(r.ok)
            self.assertIsNone(r.session_id)

    def test_http_error_does_not_echo_secret_body(self):
        with self.transport(b'test-key private text', status=401):
            r = list(api.run('问', profile=self.p))[-1]
        self.assertIn('401', r.error)
        self.assertNotIn('test-key', r.error)

    def test_changed_model_and_missing_history_require_restart(self):
        with self.transport(chat()):
            r = list(api.run('问', profile=self.p))[-1]
        self.p.model = 'new-model'
        self.assertIn('resume', list(api.run('问', profile=self.p, resume=r.session_id))[-1].error)
        self.assertIn('resume', list(api.run('问', profile=self.p, resume='missing'))[-1].error)

    def test_unsupported_image_does_not_send(self):
        self.p.supports_images = False
        with self.transport(chat()):
            r = list(api.run('问', profile=self.p, images=[b'png']))[-1]
        self.assertFalse(r.ok)
        self.assertFalse(self.sent)

    def test_stopped_before_start_does_not_send(self):
        stop = threading.Event()
        stop.set()
        with self.transport(chat()):
            r = list(api.run('问', profile=self.p, stop=stop))[-1]
        self.assertEqual(r.error, '已停止')
        self.assertFalse(self.sent)

    def test_stop_interrupts_wait_for_first_token(self):
        stop = threading.Event()
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(5)
                yield chat()
        real = httpx.AsyncClient
        transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=Slow()))
        timer = threading.Timer(0.15, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with mock.patch.object(api.httpx, 'AsyncClient', side_effect=lambda **kw: real(transport=transport, **kw)):
                r = list(api.run('问', profile=self.p, stop=stop))[-1]
        finally:
            timer.cancel()
        self.assertEqual(r.error, '已停止')
        self.assertLess(time.monotonic() - started, 1.5)

    def test_ask_once_does_not_keep_session(self):
        with self.transport(chat()):
            r = api.ask_once('出题', profile=self.p, system='规则')
        self.assertTrue(r.ok)
        self.assertIsNone(r.session_id)
        self.assertFalse(api._sessions)


if __name__ == '__main__':
    unittest.main()
