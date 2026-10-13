"""跟 Claude 说话：Claude Agent SDK（Python），走 reader **专用的 Claude 账号**（独立登录目录）。

踩过的坑：
- 用机器上装的 claude.exe（~/.local/bin，跟着 Claude Code 自动更新）。SDK 自带的那份
  太旧，不认识 claude-opus-5-5（2026-09-22 实测报 unrecognized_model）。
- SDK 起 claude.exe 不带 creationflags，pythonw 下每说一句话都弹一个黑色终端窗口 ——
  包一层 anyio.open_process 补上 CREATE_NO_WINDOW。
- `tools=[]`、`setting_sources=[]`、中性空目录当 cwd：不开 Bash/Read，不读 CLAUDE.md、
  记忆、hooks、MCP。claude CLI 本质是 agent，不这样做人格会被冲淡。

专用账号：`CLAUDE_CONFIG_DIR` 指向 %USERPROFILE%\\.claude-reader，登录跟平常的 ~/.claude
完全分开（2026-09-24 实测：换目录后 `claude auth status` 显示 loggedIn=false）。

SDK 是 async 的；这里用私有事件循环把它变成同步生成器，界面在工作线程里调。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MODEL = "claude-opus-5-5"
CONFIG_DIR = Path(os.environ.get("READER_CLAUDE_CONFIG_DIR",
                                 str(Path.home() / ".claude-reader")))
# 中性空目录：claude 在这里跑，没有 CLAUDE.md、没有项目文件
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "reader" / "claude-cwd"


def config_dir() -> Path:
    from . import config
    p = config.selected()
    return Path(p.claude_config_dir).expanduser() if p.protocol == 'claude_code' and p.claude_config_dir else CONFIG_DIR


def work_dir() -> Path:
    from .config import data_dir
    return data_dir() / 'claude-cwd'

LOGIN_HINT = ("左边的 AI 用的是 reader 专用的 Claude 账号，还没登录。"
              "在 PowerShell 里依次运行：\n"
              "$env:CLAUDE_CONFIG_DIR=\"$HOME\\.claude-reader\"\n"
              "claude auth login\n"
              "浏览器里登录 reader 专用的账号，登录完回来点「重试」。")


@dataclass
class Result:
    text: str = ""
    error: str | None = None
    session_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class Delta:
    """流式的一小段回答（界面边收边显示）。"""
    text: str


def cli_path() -> str | None:
    env = os.environ.get("CLAUDE_CLI", "")
    if not env:
        from . import config
        p = config.selected()
        env = p.cli_path if p.protocol == 'claude_code' else ''
    if env.lower().endswith(".exe") and os.path.isfile(env):
        return env
    home_exe = Path.home() / ".local" / "bin" / "claude.exe"
    if home_exe.is_file():
        return str(home_exe)
    found = shutil.which("claude.exe") or shutil.which("claude")
    return found


def child_env() -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(config_dir())}


def auth_status(timeout: int = 30) -> dict:
    """专用账号登录了没有。返回 `claude auth status` 的 JSON；出错时 {"loggedIn": False, "error": ...}。"""
    exe = cli_path()
    if not exe:
        return {"loggedIn": False, "error": "找不到 claude 命令（Claude Code 没装？）"}
    env = {**os.environ, **child_env()}
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout or "{}") or {"loggedIn": False}
    except Exception as e:  # noqa: BLE001
        return {"loggedIn": False, "error": f"{type(e).__name__}: {e}"}


_patched = False
_patch_lock = threading.Lock()


def _hide_console_windows() -> None:
    global _patched
    with _patch_lock:
        if _patched or os.name != "nt":
            _patched = True
            return
        import anyio
        orig = anyio.open_process

        async def open_process(*args, **kw):
            if not kw.get("creationflags"):
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            return await orig(*args, **kw)

        open_process.__wrapped__ = orig
        anyio.open_process = open_process
        _patched = True


_END = object()
_AUTH_WORDS = ("authenticat", "oauth", "log in", "login", "not logged", "/login")


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(w in low for w in _AUTH_WORDS)


def _image_prompt(prompt: str, images: list[bytes]):
    """带图的一轮：SDK 的流式输入（一条 user 消息，内容 = 图片 + 文字）。"""
    import base64
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(png).decode()}} for png in images]
    content.append({"type": "text", "text": prompt})

    async def gen():
        yield {"type": "user", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None}
    return gen()


def run(prompt: str, *, system: str | None = None, resume: str | None = None,
        model: str = MODEL, timeout: int = 240, stop: threading.Event | None = None,
        images: list[bytes] | None = None) -> Iterator[Delta | Result]:
    """说一轮。先逐段 yield Delta，最后 yield 一个 Result（一定有，而且只有一个）。

    resume：上一轮的 session_id，续同一个对话（系统提示只在第一轮给）。
    stop：界面点「停」时 set，这一轮尽快收尾。
    images：PNG 字节（扫描版问图表时，把那一页发给它看）。
    """
    try:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ResultMessage,
                                      TextBlock, query)
        from claude_agent_sdk.types import StreamEvent
    except ImportError:
        yield Result(error="没装 claude-agent-sdk：pip install claude-agent-sdk")
        return
    _hide_console_windows()
    cwd = work_dir()
    cwd.mkdir(parents=True, exist_ok=True)
    config_dir().mkdir(parents=True, exist_ok=True)

    opts = ClaudeAgentOptions(
        system_prompt=system if not resume else None,
        tools=[],
        allowed_tools=[],
        setting_sources=[],
        permission_mode="default",
        include_partial_messages=True,
        max_turns=1,
        cwd=str(cwd),
        resume=resume,
        model=model,
        env=child_env(),
        **({"cli_path": exe} if (exe := cli_path()) else {}),
    )

    t0 = time.time()
    loop = asyncio.new_event_loop()
    agen = None
    pending = None
    streamed: list[str] = []
    final: Result | None = None
    try:
        asyncio.set_event_loop(loop)
        agen = query(prompt=_image_prompt(prompt, images) if images else prompt,
                     options=opts).__aiter__()

        async def _next():
            try:
                return await agen.__anext__()
            except StopAsyncIteration:
                return _END

        deadline = t0 + timeout
        while True:
            if stop is not None and stop.is_set():
                final = Result(text="".join(streamed), error="已停止")
                break
            remain = deadline - time.time()
            if remain <= 0:
                final = Result(text="".join(streamed), error=f"Claude 超时（{timeout} 秒）")
                break
            if pending is None:
                pending = loop.create_task(_next())
            done, _ = loop.run_until_complete(asyncio.wait({pending}, timeout=min(remain, 0.5)))
            if not done:
                continue
            msg, pending = pending.result(), None
            if msg is _END:
                break
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                if ev.get("type") == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        streamed.append(d["text"])
                        yield Delta(d["text"])
            elif isinstance(msg, AssistantMessage):
                err = getattr(msg, "error", None)
                if err:
                    final = Result(error=str(err))
                elif not streamed:
                    # 没收到流式片段（比如 SDK 关了 partial）：整段补发
                    text = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                    if text:
                        streamed.append(text)
                        yield Delta(text)
            elif isinstance(msg, ResultMessage):
                u = msg.usage or {}
                err = None
                if msg.is_error or msg.subtype != "success":
                    err = str(msg.result or msg.subtype or "Claude 报错")[:400]
                final = Result(text="" if err else str(msg.result or "".join(streamed)),
                               error=err, session_id=msg.session_id,
                               tokens_in=int(u.get("input_tokens", 0) or 0),
                               tokens_out=int(u.get("output_tokens", 0) or 0))
    except Exception as e:  # noqa: BLE001
        if final is None:
            final = Result(text="".join(streamed), error=f"{type(e).__name__}: {e}")
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            try:
                loop.run_until_complete(asyncio.wait({pending}, timeout=10))
            except Exception:  # noqa: BLE001
                pass
        try:
            if agen is not None:
                loop.run_until_complete(agen.aclose())
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        loop.close()
        asyncio.set_event_loop(None)

    if final is None:
        final = Result(text="".join(streamed),
                       error=None if streamed else "Claude 没有返回结果")
    if final.error and _is_auth_error(final.error):
        final.error = LOGIN_HINT
    final.latency_ms = int((time.time() - t0) * 1000)
    yield final


def ask_once(prompt: str, *, system: str, timeout: int = 240,
             stop: threading.Event | None = None, model: str = MODEL) -> Result:
    """一次性问答（出题、判题、写笔记用）：不流式，只要最后结果。"""
    res = Result(error="Claude 没有返回结果")
    for ev in run(prompt, system=system, timeout=timeout, stop=stop, model=model):
        if isinstance(ev, Result):
            res = ev
    return res
