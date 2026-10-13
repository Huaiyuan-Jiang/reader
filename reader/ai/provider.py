"""问答、小测和笔记统一选择模型，不在接口失败时悄悄换服务。"""

from __future__ import annotations

from . import api, claude, config, harness, mimo
from .claude import Result


def profile() -> config.Profile:
    return config.selected(legacy_mimo=mimo.available())


def name() -> str:
    return profile().id


def label() -> str:
    """给人看的名字。"""
    try:
        return profile().label
    except config.ConfigError:
        return 'AI 配置'


def needs_login() -> bool:
    try:
        return profile().protocol == 'claude_code'
    except config.ConfigError:
        return False


def logged_in() -> bool:
    try:
        p = profile()
        if p.protocol == 'local_harness':
            return harness.ready(p)
        return bool(claude.auth_status().get('loggedIn')) if p.protocol == 'claude_code' else api.ready(p)
    except (config.ConfigError, OSError):
        return False


def setup_error() -> str:
    try:
        p = profile()
        if p.protocol == 'local_harness':
            return '' if harness.ready(p) else '本地 Harness 尚未接入，请在 AI 设置里复制接入提示词。'
        if p.protocol != 'claude_code' and not api.ready(p):
            return f'请在 AI 设置中填写 {p.label} 的 API Key'
    except (config.ConfigError, OSError) as e:
        return str(e)
    return ''


def run(prompt, **kw):
    try:
        p = profile()
        if p.protocol == 'claude_code':
            kw.setdefault('model', p.model)
            kw.setdefault('timeout', p.timeout)
            yield from claude.run(prompt, **kw)
        elif p.protocol == 'local_harness':
            yield from harness.run(prompt, profile=p, **kw)
        else:
            yield from api.run(prompt, profile=p, **kw)
    except (config.ConfigError, OSError) as e:
        yield Result(error=str(e))


def ask_once(prompt, **kw):
    try:
        p = profile()
        if p.protocol == 'claude_code':
            kw.setdefault('model', p.model)
            kw.setdefault('timeout', p.timeout)
            return claude.ask_once(prompt, **kw)
        if p.protocol == 'local_harness':
            return harness.ask_once(prompt, profile=p, **kw)
        return api.ask_once(prompt, profile=p, **kw)
    except (config.ConfigError, OSError) as e:
        return Result(error=str(e))
