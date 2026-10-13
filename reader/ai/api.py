"""三个 API 协议共用的流式传输，保持 Delta / Result 接口。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict
from urllib.parse import urlsplit

import httpx
from . import secrets
from .claude import Delta, Result
from .config import Profile

_sessions = OrderedDict()
_lock = threading.Lock()
_END = object()


def key_for(p: Profile) -> str:
    key = os.environ.get(p.api_key_env, '').strip() if p.api_key_env else ''
    if key:
        return key
    return secrets.get(p.id)


def ready(p: Profile) -> bool:
    return bool(key_for(p)) or urlsplit(p.base_url).hostname in ('localhost', '127.0.0.1', '::1')


def _fingerprint(p, key):
    return hashlib.sha256((json.dumps(asdict(p), sort_keys=True) + key).encode()).hexdigest()


def _content(prompt, images, protocol):
    if not images:
        return prompt
    if protocol == 'anthropic_messages':
        return [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                'data': base64.b64encode(b).decode()}} for b in images] + [{'type': 'text', 'text': prompt}]
    if protocol == 'responses':
        return [{'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(b).decode()}
                for b in images] + [{'type': 'input_text', 'text': prompt}]
    return [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(b).decode()}}
            for b in images] + [{'type': 'text', 'text': prompt}]


def _request(p, key, messages):
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    if key:
        headers[p.auth_header] = 'Bearer ' + key if p.auth_header.lower() == 'authorization' else key
    body = dict(p.extra_body)
    if p.protocol == 'responses':
        body.update(model=p.model, input=messages, max_output_tokens=p.max_tokens, stream=True, store=False)
        suffix = '/responses'
    elif p.protocol == 'anthropic_messages':
        headers['anthropic-version'] = '2023-06-01'
        sysmsgs = [m['content'] for m in messages if m['role'] == 'system']
        body.update(model=p.model, messages=[m for m in messages if m['role'] != 'system'],
                    max_tokens=p.max_tokens, stream=True)
        if sysmsgs:
            body['system'] = '\n\n'.join(sysmsgs)
        suffix = '/messages'
    else:
        body.update(model=p.model, messages=messages, stream=True, stream_options={'include_usage': True})
        body[p.token_parameter] = p.max_tokens
        suffix = '/chat/completions'
    return p.base_url.rstrip('/') + suffix, headers, body


async def _events(url, headers, body, timeout):
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(15, timeout)),
                                follow_redirects=False) as client:
        async with client.stream('POST', url, headers=headers, json=body) as response:
            if response.status_code >= 300:
                yield {'_http_error': response.status_code}
                return
            data = []
            async for line in response.aiter_lines():
                if not line:
                    if data:
                        raw, data = '\n'.join(data), []
                        if raw == '[DONE]':
                            yield {'_done': True}
                            return
                        yield json.loads(raw)
                elif line.startswith('data:'):
                    data.append(line[5:].lstrip())
            if data:
                raw = '\n'.join(data)
                yield {'_done': True} if raw == '[DONE]' else json.loads(raw)


def _http_error(label, status):
    if status in (401, 403):
        return f'{label} 的 API Key 无效或没有访问权限（{status}）'
    if status == 402:
        return f'{label} 账户额度不足（402）'
    if status == 429:
        return f'{label} 请求过多或额度受限（429），请稍后重试'
    return f'{label} 接口返回 {status}，请检查地址、模型 ID 和账户状态'


def run(prompt: str, *, profile: Profile, system=None, resume=None, model=None,
        timeout=None, stop=None, images=None):
    p = Profile(**asdict(profile))
    if model is not None:
        p.model = model
    if timeout is not None:
        p.timeout = timeout
    p.validate()
    t0 = time.monotonic()
    if stop is not None and stop.is_set():
        yield Result(error='已停止')
        return
    if images and not p.supports_images:
        yield Result(error=f'{p.label} 当前模型未开启图片支持。可问书中文字，或在 AI 设置中换模型。')
        return
    key = key_for(p)
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in key):
        yield Result(error='API Key 含换行或其他无效字符，请重新粘贴完整的一行 Key')
        return
    if not key and not ready(p):
        yield Result(error=f'请在 AI 设置中填写 {p.label} 的 API Key，或设置 {p.api_key_env}')
        return
    fingerprint = _fingerprint(p, key)
    with _lock:
        record = _sessions.get(resume) if resume else None
        past = list(record[1]) if record and record[0] == fingerprint else []
    if resume and not past:
        yield Result(error='resume 失败：模型设置已变化或会话已经结束，请重新提问')
        return
    msgs = list(past)
    if not msgs and system:
        msgs.append({'role': 'system', 'content': system})
    user = {'role': 'user', 'content': _content(prompt, images, p.protocol)}
    url, headers, body = _request(p, key, msgs + [user])
    loop = asyncio.new_event_loop()
    agen = _events(url, headers, body, p.timeout).__aiter__()
    pending = None
    streamed, reasoning = [], []
    tokens_in = tokens_out = 0
    final = None
    completed = False
    try:
        async def next_event():
            try:
                return await agen.__anext__()
            except StopAsyncIteration:
                return _END
        while True:
            if stop is not None and stop.is_set():
                final = Result(error='已停止')
                break
            remain = p.timeout - (time.monotonic() - t0)
            if remain <= 0:
                final = Result(error=f'{p.label} 超时（{p.timeout} 秒）')
                break
            if pending is None:
                pending = loop.create_task(next_event())
            done, _ = loop.run_until_complete(asyncio.wait({pending}, timeout=min(0.2, remain)))
            if not done:
                continue
            ev, pending = pending.result(), None
            if ev is _END:
                break
            if not isinstance(ev, dict):
                raise ValueError('invalid stream event')
            if '_http_error' in ev:
                final = Result(error=_http_error(p.label, ev['_http_error']))
                break
            if ev.get('error') or ev.get('type') in ('error', 'response.failed'):
                final = Result(error=f'{p.label} 接口返回错误，请检查模型和额度')
                break
            piece = ''
            if p.protocol == 'chat_completions':
                usage = ev.get('usage') or {}
                tokens_in = int(usage.get('prompt_tokens', tokens_in) or 0)
                tokens_out = int(usage.get('completion_tokens', tokens_out) or 0)
                for ch in ev.get('choices') or []:
                    delta = ch.get('delta') or {}
                    piece += delta.get('content') or ''
                    if delta.get('reasoning_content'):
                        reasoning.append(delta['reasoning_content'])
                    reason = ch.get('finish_reason')
                    if reason in ('length', 'content_filter', 'tool_calls'):
                        final = Result(error=f'{p.label} 回答未完成（{reason}），请调整输出上限或问题')
                    elif reason:
                        completed = True
                completed = completed or bool(ev.get('_done'))
            elif p.protocol == 'responses':
                typ = ev.get('type')
                if typ == 'response.output_text.delta':
                    piece = ev.get('delta') or ''
                elif typ == 'response.completed':
                    r = ev.get('response') or {}
                    u = r.get('usage') or {}
                    tokens_in, tokens_out = int(u.get('input_tokens') or 0), int(u.get('output_tokens') or 0)
                    completed = r.get('status', 'completed') == 'completed'
                elif typ == 'response.incomplete':
                    final = Result(error=f'{p.label} 回答未完成，请调整输出上限或问题')
            else:
                typ = ev.get('type')
                if typ == 'message_start':
                    tokens_in = int(((ev.get('message') or {}).get('usage') or {}).get('input_tokens') or 0)
                elif typ == 'content_block_delta' and (ev.get('delta') or {}).get('type') == 'text_delta':
                    piece = ev['delta'].get('text') or ''
                elif typ == 'message_delta':
                    tokens_out = int((ev.get('usage') or {}).get('output_tokens') or tokens_out)
                    if (ev.get('delta') or {}).get('stop_reason') not in (None, 'end_turn', 'stop_sequence'):
                        final = Result(error=f'{p.label} 回答未完成，请调整输出上限或问题')
                elif typ == 'message_stop':
                    completed = True
            if piece:
                streamed.append(piece)
                yield Delta(piece)
            if final is not None:
                break
    except httpx.TimeoutException:
        final = Result(error=f'{p.label} 连接超时，请检查网络或稍后重试')
    except httpx.HTTPError:
        final = Result(error=f'连不上 {p.label}，请检查接口地址和网络')
    except (ValueError, TypeError, KeyError, AttributeError):
        final = Result(error=f'{p.label} 返回格式与接口类型不匹配，请检查 AI 设置')
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            loop.run_until_complete(asyncio.gather(pending, return_exceptions=True))
        loop.run_until_complete(agen.aclose())
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    text = ''.join(streamed)
    if final is None:
        final = Result(error=None if completed and text.strip() else f'{p.label} 没有完整返回结果，请重试')
    final.text = text
    final.tokens_in, final.tokens_out = tokens_in, tokens_out
    final.latency_ms = int((time.monotonic() - t0) * 1000)
    if final.ok:
        sid = resume or uuid.uuid4().hex
        kept = {'role': 'user', 'content': prompt + ('\n（当时附了一张图片）' if images else '')}
        assistant = {'role': 'assistant', 'content': text}
        if reasoning and p.protocol == 'chat_completions':
            assistant['reasoning_content'] = ''.join(reasoning)
        conv = msgs + [kept, assistant]
        head = conv[:1] if conv[0]['role'] == 'system' else []
        rest = conv[len(head):]
        if len(rest) > 40:
            rest = rest[:2] + rest[-38:]
        with _lock:
            _sessions[sid] = (fingerprint, head + rest)
            _sessions.move_to_end(sid)
            while len(_sessions) > 32:
                _sessions.popitem(last=False)
        final.session_id = sid
    yield final


def ask_once(prompt, *, profile, system, **kw):
    result = Result(error='没有返回结果')
    for event in run(prompt, profile=profile, system=system, **kw):
        if isinstance(event, Result):
            result = event
    if result.session_id:
        with _lock:
            _sessions.pop(result.session_id, None)
        result.session_id = None
    return result
