"""A temporary Windows Alt+Tab guard, scoped to the foreground reader process."""
from __future__ import annotations

import atexit
import ctypes
import os
import sys
from ctypes import wintypes

_guards = set()


class AltTabGuard:
    def __init__(self):
        self._hook = None
        self._callback = None
        self._blocked_tab = False

    def should_block(self, message: int, key: int, flags: int, foreground: bool) -> bool:
        if key != 9:
            return False
        if message in (0x100, 0x104) and flags & 0x20 and foreground:
            self._blocked_tab = True
            return True
        if message in (0x101, 0x105) and self._blocked_tab:
            self._blocked_tab = False
            return True
        return False

    def start(self) -> bool:
        if self._hook:
            return True
        if sys.platform != 'win32' or os.environ.get('READER_NO_LOCK') or \
                os.environ.get('QT_QPA_PLATFORM') == 'offscreen':
            return False
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM)

        class Key(ctypes.Structure):
            _fields_ = [('vk', wintypes.DWORD), ('scan', wintypes.DWORD),
                        ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                        ('extra', ctypes.c_size_t)]

        user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.UnhookWindowsHookEx.restype = wintypes.BOOL
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        self._user = user

        def callback(code, message, address):
            if code >= 0:
                try:
                    event = ctypes.cast(address, ctypes.POINTER(Key)).contents
                    pid = wintypes.DWORD()
                    user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(pid))
                    if self.should_block(message, event.vk, event.flags, pid.value == os.getpid()):
                        return 1
                except Exception:
                    pass
            return user.CallNextHookEx(self._hook, code, message, address)

        self._callback = callback_type(callback)
        self._hook = user.SetWindowsHookExW(13, self._callback, kernel.GetModuleHandleW(None), 0)
        if not self._hook:
            self._callback = None
            raise OSError(ctypes.get_last_error(), '无法启用阅读期间的 Alt+Tab 限制')
        _guards.add(self)
        return True

    def stop(self, *_):
        if self._hook:
            if not self._user.UnhookWindowsHookEx(self._hook):
                raise OSError(ctypes.get_last_error(), '无法恢复 Alt+Tab')
            self._hook = None
        self._callback = None
        self._blocked_tab = False
        _guards.discard(self)


def _release_all():
    for guard in tuple(_guards):
        guard.stop()


atexit.register(_release_all)
