"""Windows DPAPI 保护本机 API Key，JSON 配置不携带凭据。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import shutil
from datetime import datetime
from ctypes import wintypes

from .config import ConfigError, atomic_json, data_dir

_lock = threading.RLock()


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise ConfigError('保存 API Key 需要 Windows；其他系统请使用环境变量')
    buf = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    incoming = _Blob(len(value), buf)
    outgoing = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                  ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ConfigError('Windows 无法保护或读取 API Key，请重新填写')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


def _load() -> dict:
    path = data_dir() / 'ai-secrets.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             for k, v in data.items()):
            raise ValueError()
        return data
    except (OSError, ValueError) as e:
        raise ConfigError('本机凭据文件无法读取，请重新配置 API Key') from e


def get(profile_id: str) -> str:
    with _lock:
        value = _load().get(profile_id)
        if not value:
            return ''
        try:
            return _crypt(base64.b64decode(value, validate=True), decrypt=True).decode('utf-8')
        except (ValueError, UnicodeError) as e:
            raise ConfigError('本机 API Key 无法读取，请重新填写') from e


def put(profile_id: str, key: str) -> None:
    with _lock:
        try:
            data = _load()
        except ConfigError:
            path = data_dir() / 'ai-secrets.json'
            backup = path.with_name(path.name + '.bad-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            try:
                shutil.copy2(path, backup)
            except OSError as e:
                raise ConfigError('无法备份损坏的凭据文件，请检查数据目录的写入权限') from e
            data = {}
        if key.strip():
            data[profile_id] = base64.b64encode(_crypt(key.strip().encode('utf-8'))).decode('ascii')
        else:
            data.pop(profile_id, None)
        atomic_json(data_dir() / 'ai-secrets.json', data)
