"""可分享的模型设置。凭据独立存放，环境变量优先于本机设置。"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOLS = ('claude_code', 'responses', 'chat_completions', 'anthropic_messages', 'local_harness')


class ConfigError(ValueError):
    pass


def data_dir() -> Path:
    return Path(os.environ.get('READER_DATA_DIR') or
                Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'reader')


def config_path() -> Path:
    return Path(os.environ.get('READER_AI_CONFIG') or data_dir() / 'ai-config.json')


@dataclass
class Profile:
    id: str
    label: str
    protocol: str
    model: str
    base_url: str = ''
    api_key_env: str = ''
    auth_header: str = 'Authorization'
    supports_images: bool = True
    max_tokens: int = 8192
    timeout: int = 240
    token_parameter: str = 'max_tokens'
    extra_body: dict = field(default_factory=dict)
    claude_config_dir: str = ''
    cli_path: str = ''
    harness_command: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', self.id):
            raise ConfigError('配置 ID 只能包含小写字母、数字、下划线和连字符')
        if self.protocol not in PROTOCOLS:
            raise ConfigError('接口类型不支持')
        if not self.label.strip() or not self.model.strip():
            raise ConfigError('请填写名称和模型 ID')
        if not isinstance(self.timeout, int) or not 1 <= self.timeout <= 1800:
            raise ConfigError('超时需在 1 到 1800 秒之间')
        if not isinstance(self.max_tokens, int) or not 16 <= self.max_tokens <= 131072:
            raise ConfigError('输出上限需在 16 到 131072 之间')
        if not isinstance(self.supports_images, bool):
            raise ConfigError('图片支持需填写 true 或 false')
        if self.api_key_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.api_key_env):
            raise ConfigError('API Key 环境变量名无效')
        if not re.fullmatch(r'[A-Za-z0-9-]+', self.auth_header):
            raise ConfigError('认证头名称无效')
        if self.token_parameter not in ('max_tokens', 'max_completion_tokens'):
            raise ConfigError('输出参数需为 max_tokens 或 max_completion_tokens')
        if not isinstance(self.extra_body, dict):
            raise ConfigError('附加请求参数需为 JSON 对象')
        forbidden = {'model', 'messages', 'input', 'instructions', 'stream', 'stream_options',
                     'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'system',
                     'api_key', 'authorization', 'headers', 'store'}
        if forbidden.intersection(k.lower() for k in self.extra_body):
            raise ConfigError('附加参数不能覆盖模型、正文、凭据或流式设置')
        if not isinstance(self.harness_command, list) or any(not isinstance(a, str) or not a or '\x00' in a for a in self.harness_command):
            raise ConfigError('本地 Harness 命令需要字符串数组')
        if self.protocol not in ('claude_code', 'local_harness'):
            u = urlsplit(self.base_url)
            local = u.hostname in ('localhost', '127.0.0.1', '::1')
            if not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ConfigError('请填写不含凭据、查询参数的接口根地址')
            if u.scheme != 'https' and not (u.scheme == 'http' and local):
                raise ConfigError('远程接口需使用 HTTPS；本机接口可以使用 HTTP')


def defaults() -> dict:
    profiles = [
        Profile('claude', 'Claude Code', 'claude_code', 'claude-opus-5-5',
                claude_config_dir='~/.claude-reader'),
        Profile('openai', 'GPT / OpenAI', 'responses', 'gpt-6-astra',
                'https://api.openai.com/v1', 'OPENAI_API_KEY'),
        Profile('deepseek', 'DeepSeek', 'chat_completions', 'deepseek-flash',
                'https://api.deepseek.com', 'DEEPSEEK_API_KEY',
                extra_body={'thinking': {'type': 'disabled'}}),
        Profile('anthropic', 'Claude API', 'anthropic_messages', 'claude-opus-5-5',
                'https://api.anthropic.com/v1', 'ANTHROPIC_API_KEY', auth_header='x-api-key'),
        Profile('mimo', 'MiMo', 'chat_completions', 'mimo-v2.6-pro',
                'https://api.xiaomimimo.com/v1', 'READER_MIMO_KEY', auth_header='api-key',
                token_parameter='max_completion_tokens', extra_body={'thinking': {'type': 'disabled'}}),
        Profile('custom', '自定义兼容接口', 'chat_completions', 'your-model-id',
                'http://localhost:11434/v1', 'READER_CUSTOM_KEY', supports_images=False),
        Profile('harness', '本地 Harness', 'local_harness', 'local-harness', supports_images=False),
    ]
    return {'version': 1, 'active': 'auto', 'profiles': [asdict(p) for p in profiles]}


def validate(data: dict) -> dict[str, Profile]:
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ConfigError('AI 配置版本不支持（需要 version=1）')
    result = {}
    try:
        for raw in data['profiles']:
            p = Profile(**raw)
            p.validate()
            if p.id in result:
                raise ConfigError('配置 ID 重复')
            result[p.id] = p
    except (KeyError, TypeError, AttributeError) as e:
        raise ConfigError('AI 配置格式有误，请打开 AI 设置检查') from e
    if not result or data.get('active') not in (*result, 'auto'):
        raise ConfigError('当前选中的 AI 配置不存在')
    return result


def load() -> dict:
    path = config_path()
    if not path.exists():
        return defaults()
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as e:
        raise ConfigError(f'AI 配置无法读取，请检查 {path.name}') from e
    validate(data)
    return data


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(data: dict) -> None:
    validate(data)
    atomic_json(config_path(), data)


def selected(*, legacy_mimo: bool = False) -> Profile:
    data = load()
    profiles = validate(data)
    chosen = os.environ.get('READER_AI', '').strip().lower() or data['active']
    chosen = {'claude-code': 'claude', 'claude_code': 'claude', 'gpt': 'openai'}.get(chosen, chosen)
    if chosen == 'auto':
        chosen = 'mimo' if legacy_mimo and 'mimo' in profiles else 'claude'
    if chosen not in profiles:
        raise ConfigError(f'找不到 AI 配置 {chosen}，请在 AI 设置中选择')
    p = profiles[chosen]
    overrides = {'model': os.environ.get('READER_MODEL'), 'base_url': os.environ.get('READER_BASE_URL'),
                 'claude_config_dir': os.environ.get('READER_CLAUDE_CONFIG_DIR'),
                 'cli_path': os.environ.get('CLAUDE_CLI')}
    for k, v in overrides.items():
        if v:
            setattr(p, k, v)
    p.validate()
    return p
