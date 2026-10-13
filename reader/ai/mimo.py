"""跟小米 MiMo 说话：MiMo-V2.6-Pro，OpenAI 兼容接口（填自己的 MiMo Key 就能用，不用登 Claude）。

- 接口：POST https://api.xiaomimimo.com/v1/chat/completions，key 放在 `api-key` 头里（不是 Authorization）。
  文档：https://mimo.mi.com/docs/en-US/quick-start/summary/first-api-call
- key：环境变量 READER_MIMO_KEY，或在 AI 设置里填（加密存在本机）。旧版的本机文件
  `reader/ai/_mimo_key.py` 只为兼容，不进 git，也不进源码包和发行包。
- 接口是无状态的：多轮对话在这边记（session_id → 消息列表），跟 claude.run 的 resume 用法一样。
- 思考：问答关掉（`thinking: disabled`，第一个字就能流出来）；出题、写笔记开着（慢一点，题出得好）。
  开着思考时，文档要求把 reasoning_content 留在后面几轮的消息里 —— 一次性问答没有后面几轮，不用管。
- 跟 claude.run / ask_once 同样的签名和返回（Delta… 最后一个 Result），界面不用分是谁在答。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Iterator

from .claude import Delta, Result

MODEL = "mimo-v2.6-pro"
URL = "https://api.xiaomimimo.com/v1/chat/completions"
MAX_OUT = 8192
HISTORY_TURNS = 20            # 一个对话最多记这么多轮（原文只在第一次问到那一节时发，别丢了开头）

_sessions: dict[str, list[dict]] = {}
_lock = threading.Lock()


def api_key() -> str:
    env = os.environ.get("READER_MIMO_KEY", "").strip()
    if env:
        return env
    try:
        from . import _mimo_key
        return str(getattr(_mimo_key, "KEY", "") or "").strip()
    except ImportError:
        return ""


def available() -> bool:
    return bool(api_key())


def _friendly(status: int, body: str) -> str:
    low = body.lower()
    if status in (401, 403):
        return "MiMo 的 key 不对或者被停用了（401/403）"
    if status == 402 or "balance" in low or "insufficient" in low or "余额" in body:
        return "MiMo 的额度用完了（账户余额不足）"
    if status == 429:
        return "MiMo 这会儿请求太多（429，rate limit），过一会儿再试"
    if status >= 500:
        return f"MiMo 服务器出错了（{status}），过一会儿再试"
    return f"MiMo 报错 {status}：{body[:300]}"


def _user_content(prompt: str, images: list[bytes] | None):
    if not images:
        return prompt
    parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}
             for png in images]
    parts.append({"type": "text", "text": prompt})
    return parts


def run(prompt: str, *, system: str | None = None, resume: str | None = None,
        model: str = MODEL, timeout: int = 240, stop: threading.Event | None = None,
        images: list[bytes] | None = None, thinking: bool = False) -> Iterator[Delta | Result]:
    """说一轮。先逐段 yield Delta，最后 yield 一个 Result（一定有，而且只有一个）。

    resume：上一轮的 session_id，续同一个对话（系统提示只在第一轮给）。
    stop：界面点「停」时 set，这一轮尽快收尾（下一个片段到了就停）。
    images：PNG 字节（扫描版问图表时，把那一页发给它看）。
    """
    t0 = time.time()
    key = api_key()
    if not key:
        yield Result(error="没有 MiMo 的 key：在 AI 设置里填，或设置环境变量 READER_MIMO_KEY")
        return
    with _lock:
        past = list(_sessions.get(resume, [])) if resume else []
    if resume and not past:
        yield Result(error="resume 失败：这个对话已经不在了")   # Companion 见到 resume 会重开、重发原文
        return
    msgs = list(past)
    if not msgs and system:
        msgs.append({"role": "system", "content": system})
    user = {"role": "user", "content": _user_content(prompt, images)}
    body = {"model": model, "messages": msgs + [user], "max_completion_tokens": MAX_OUT, "stream": True,
            "stream_options": {"include_usage": True}}
    if not thinking:
        body["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "api-key": key,
                                          "Accept": "text/event-stream"})
    streamed: list[str] = []
    usage: dict = {}
    final: Result | None = None
    deadline = t0 + timeout
    try:
        with urllib.request.urlopen(req, timeout=min(60, timeout)) as resp:
            for raw in resp:
                if stop is not None and stop.is_set():
                    final = Result(text="".join(streamed), error="已停止")
                    break
                if time.time() > deadline:
                    final = Result(text="".join(streamed), error=f"MiMo 超时（{timeout} 秒）")
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if ev.get("error"):
                    final = Result(text="".join(streamed), error=f"MiMo 报错：{ev['error']}")
                    break
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        streamed.append(piece)
                        yield Delta(piece)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        final = Result(text="".join(streamed), error=_friendly(e.code, detail))
    except (socket.timeout, TimeoutError):
        final = Result(text="".join(streamed), error=f"MiMo 超时（{timeout} 秒）")
    except urllib.error.URLError as e:
        final = Result(text="".join(streamed), error=f"连不上 MiMo（{e.reason}），看看网络")
    except Exception as e:  # noqa: BLE001
        final = Result(text="".join(streamed), error=f"{type(e).__name__}: {e}")

    text = "".join(streamed)
    if final is None:
        final = Result(text=text, error=None if text.strip() else "MiMo 没有返回结果")
    if final.ok:
        sid = resume or uuid.uuid4().hex
        # 图片不留在历史里（下一轮不用再传一遍那页图），换成一句说明
        kept = {"role": "user", "content": prompt + ("\n（当时附了一张图）" if images else "")}
        with _lock:
            conv = msgs + [kept, {"role": "assistant", "content": text}]
            head = conv[:1] if conv and conv[0]["role"] == "system" else []
            rest = conv[len(head):]
            # 太长了丢中间的旧轮次，保留系统提示和第一轮（第一轮里常有这一节的原文）
            if len(rest) > 2 * HISTORY_TURNS:
                rest = rest[:2] + rest[-(2 * HISTORY_TURNS - 2):]
            _sessions[sid] = head + rest
        final.session_id = sid
    final.tokens_in = int(usage.get("prompt_tokens", 0) or 0)
    final.tokens_out = int(usage.get("completion_tokens", 0) or 0)
    final.latency_ms = int((time.time() - t0) * 1000)
    yield final


def ask_once(prompt: str, *, system: str, timeout: int = 240,
             stop: threading.Event | None = None) -> Result:
    """一次性问答（出题、写笔记用）：开着思考，只要最后结果，不留对话。"""
    res = Result(error="MiMo 没有返回结果")
    for ev in run(prompt, system=system, timeout=timeout, stop=stop, thinking=True):
        if isinstance(ev, Result):
            res = ev
    if res.session_id:
        with _lock:
            _sessions.pop(res.session_id, None)
        res.session_id = None
    return res
