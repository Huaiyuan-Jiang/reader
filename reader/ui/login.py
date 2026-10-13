"""登录那块（界面稿板 11）：reader 专用的 Claude 账号没登录时，一直留在书架顶上，直到登好。不弹窗。

几种状态（`LoginBanner.state`）：
- checking：刚启动，正在后台查（不显示这块，书架右下角写「正在看 AI 登录了没有」）
- out：没登录 → 大卡片：三步说明 +「打开登录窗口」+ 自己来的两行命令（一键复制）
- waiting：点了「打开登录窗口」→ 小卡片「等你在浏览器里登录」，每 4 秒自己查一次
- ok_new：刚刚登好 →「登好了」+ 账号（打码）+「知道了」
- same：登的跟平常 ~/.claude 是同一个账号 → 直说会抢额度：「退出，换个账号登」/「就用这个」
- error：查不了（比如没装 claude 命令）→ 原因 +「重新检查」
- ok：登好了，这块不显示

「打开登录窗口」：开一个 PowerShell，里面已经设好 `$env:CLAUDE_CONFIG_DIR`，直接跑 `claude auth login`。
同一个账号的判断：分别跑带 / 不带 CLAUDE_CONFIG_DIR 的 `claude auth status`，比较 email（在工作线程里）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from html import escape
from typing import Callable

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStackedLayout, QVBoxLayout, QWidget

from reader.ai import claude
from reader.ui import theme
from reader.ui.shelf_kit import (Badge, Button, Card, IconLabel, Label, background, cap, on_theme,
                                 rich, row)

POLL_MS = 4000               # 等登录时多久查一次
POLL_FOR_S = 15 * 60         # 最多自动查这么久（之后靠「重新检查」和切回窗口时查）


@dataclass
class AuthResult:
    logged_in: bool
    email: str = ""
    default_email: str = ""      # 平常 ~/.claude 那个账号（没登录 / 查不了就是 ""）
    error: str = ""

    @property
    def same_account(self) -> bool:
        return bool(self.logged_in and self.email and self.default_email
                    and self.email.strip().lower() == self.default_email.strip().lower())


def default_status(timeout: int = 30) -> dict:
    """平常那个账号（不带 CLAUDE_CONFIG_DIR）的 `claude auth status`。"""
    exe = claude.cli_path()
    if not exe:
        return {"loggedIn": False, "error": "找不到 claude 命令"}
    env = {k: v for k, v in os.environ.items() if k.upper() != "CLAUDE_CONFIG_DIR"}
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=env, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout or "{}") or {"loggedIn": False}
    except Exception as e:  # noqa: BLE001
        return {"loggedIn": False, "error": f"{type(e).__name__}: {e}"}


def probe() -> AuthResult:
    """查一次（会跑两个子进程，几秒，放在工作线程里）。用 MiMo 时不用登录，直接算登好了（这块不显示）。"""
    from reader.ai import provider
    if not provider.needs_login():
        error = provider.setup_error()
        return AuthResult(not bool(error), error=error)
    mine = claude.auth_status()
    if not mine.get("loggedIn"):
        return AuthResult(False, error=str(mine.get("error") or ""))
    base = default_status()
    return AuthResult(True, email=str(mine.get("email") or ""),
                      default_email=str(base.get("email") or "") if base.get("loggedIn") else "")


def mask_email(email: str) -> str:
    """「r•••@example.com」"""
    if "@" not in email:
        return email
    name, dom = email.split("@", 1)
    return f"{name[:1]}•••@{dom}"


def manual_commands() -> str:
    return f'$env:CLAUDE_CONFIG_DIR={_ps_literal(str(claude.config_dir()))}\nclaude auth login'


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def login_script(logout_first: bool = False) -> str:
    exe = claude.cli_path()
    call = f'& {_ps_literal(exe)}' if exe else "claude"
    lines = [
        f'$env:CLAUDE_CONFIG_DIR={_ps_literal(str(claude.config_dir()))}',
        "Write-Host '这是 reader 的登录窗口。浏览器会打开 Claude 登录页：请登 reader 专用的账号。' -ForegroundColor Yellow",
        "Write-Host '登好之后回到 reader，它会自己发现。这个窗口可以关掉。'",
        "Write-Host ''",
    ]
    if logout_first:
        lines.append(f"{call} auth logout")
    lines.append(f"{call} auth login")
    return "\n".join(lines)


def powershell_args(logout_first: bool = False) -> list[str]:
    enc = base64.b64encode(login_script(logout_first).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoExit", "-NoProfile", "-EncodedCommand", enc]


def open_login_window(logout_first: bool = False, launcher: Callable | None = None) -> None:
    """开一个新的 PowerShell 窗口去登录。launcher 给测试换掉（默认 subprocess.Popen）。"""
    args = powershell_args(logout_first)
    if launcher is not None:
        launcher(args)
        return
    subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


# ── 界面 ────────────────────────────────────────────────────
class _Steps(QWidget):
    """编号的三步（界面稿 .steps）。"""

    def __init__(self, items: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        for i, text in enumerate(items, 1):
            num = Label(str(i), 11, 400, "t3", tnum=True)
            num.setFixedSize(22, 22)
            num.setAlignment(Qt.AlignCenter)

            def _ring(n=num):
                n.setStyleSheet(f"QLabel {{ color: {theme.tok('t3')}; border: 1px solid {theme.tok('l3')}; "
                                f"border-radius: 11px; background: transparent; }}")
            _ring()
            on_theme(num, _ring)
            body = rich(text, 13, "t2", line=1.65)
            h = QHBoxLayout()
            h.setSpacing(10)
            col = QVBoxLayout()
            col.addWidget(num)
            col.addStretch(1)
            h.addLayout(col)
            h.addWidget(body, 1)
            lay.addLayout(h)


class LoginBanner(QWidget):
    """书架顶上的登录块。state_changed(state) 给书架右下角那行 AI 状态用。"""

    state_changed = Signal(str)
    settings_requested = Signal()

    def __init__(self, settings=None, *, prober: Callable[[], AuthResult] = probe,
                 launcher: Callable | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings          # Library（存「就用这个」），可以是 None
        self._probe = prober
        self._launcher = launcher
        self.state = "checking"
        self.result: AuthResult | None = None
        self._busy = False
        self._again = False
        self._waiting_since = 0.0
        self._last_check = 0.0

        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_tick)

        # 不用 QStackedLayout：它按大的那张算高度，小卡片会被撑高
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._big = self._build_big()
        self._small = self._build_small()
        lay.addWidget(self._big)
        lay.addWidget(self._small)
        self._small.setVisible(False)
        self.setVisible(False)

    # ── 对外 ────────────────────────────────────────────
    def logged_in(self) -> bool:
        return self.state in ("ok", "ok_new", "same")

    def status_line(self) -> tuple[str, str]:
        """(点的颜色键名, 书架右下角那句话)"""
        from reader.ai import provider
        if self._probe is probe and not provider.needs_login():
            return ('green', provider.label() + ' · 已配置') if self.ready else ('red', provider.label() + ' · 待配置')
        return {
            "checking": ("t5", "正在看 AI 登录了没有…"),
            "out": ("red", "AI 没登录"),
            "waiting": ("red", "AI 没登录 · 等你登录"),
            "error": ("red", "AI 暂时用不了"),
            "same": ("acc", "AI 登的是平常那个账号"),
        }.get(self.state, ("green", "AI 已登录（专用账号）"))

    def recheck(self, *, quiet: bool = False) -> None:
        """在后台查一次。quiet=True：不改按钮字（自动查的时候）。"""
        if self._busy:
            self._again = True
            return
        self._busy = True
        self._last_check = time.time()
        if not quiet:
            self._set_checking_ui(True)
        background(self._probe, self._on_probe)

    def on_window_activated(self) -> None:
        """主人切回书架（多半是登完回来了）：没登好就查一次。"""
        if self.state in ("out", "waiting", "error", "same") and time.time() - self._last_check > 3:
            self.recheck(quiet=True)

    # ── 查的结果 ────────────────────────────────────────
    def _on_probe(self, res, err) -> None:
        self._busy = False
        self._set_checking_ui(False)
        if err is not None:
            res = AuthResult(False, error=f"{type(err).__name__}: {err}")
        self.result = res
        prev = self.state
        if res.logged_in:
            accepted = self.settings.setting("auth_same_ok") if self.settings else None
            if res.same_account and (accepted or "").lower() != res.email.lower():
                new = "same"
            elif prev in ("out", "waiting", "same", "error") and not (prev == "same" and res.same_account):
                new = "ok_new"
            elif prev == "ok_new":
                new = "ok_new"
            else:
                new = "ok"
        elif res.error:          # 查不了（没装 claude、超时……），跟「没登录」分开说
            new = "waiting" if prev == "waiting" else "error"
        else:
            new = "waiting" if prev == "waiting" else "out"
        if new != "waiting":
            self._poll.stop()
        self._set_state(new)
        if self._again:
            self._again = False
            self.recheck(quiet=True)

    def _poll_tick(self) -> None:
        if time.time() - self._waiting_since > POLL_FOR_S:
            self._poll.stop()
            return
        self.recheck(quiet=True)

    # ── 按钮 ────────────────────────────────────────────
    def _open_login(self, logout_first: bool = False) -> None:
        try:
            open_login_window(logout_first, self._launcher)
        except OSError as e:
            self._set_state("error", f"PowerShell 打不开：{e}。可以照右边两行命令自己来。")
            return
        self._waiting_since = time.time()
        self._poll.start()
        self._set_state("waiting")

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(manual_commands())
        self._copy_btn.setText("已复制")
        self._copy_btn.set_icon_name("ok")
        QTimer.singleShot(2500, lambda: (self._copy_btn.setText("复制"), self._copy_btn.set_icon_name("copy")))

    def _accept_same(self) -> None:
        if self.settings is not None and self.result is not None:
            self.settings.set_setting("auth_same_ok", self.result.email)
        self._set_state("ok")

    # ── 画面 ────────────────────────────────────────────
    def _set_checking_ui(self, on: bool) -> None:
        for b in (self._recheck_big, self._recheck_small):
            b.setEnabled(not on)
            b.setText("正在检查…" if on else "重新检查")

    def _set_state(self, state: str, error_text: str = "") -> None:
        self.state = state
        from reader.ai import provider
        if self._probe is probe and not provider.needs_login():
            self._poll.stop()
            self._big.setVisible(False)
            self._small.setVisible(state not in ('ok', 'checking'))
            self.setVisible(state not in ('ok', 'checking'))
            if state not in ('ok', 'checking'):
                self._s_icon.setCurrentIndex(2)
                self._s_title.setText(provider.label() + ' 需要设置')
                self._s_body.setText(escape(error_text or (self.result.error if self.result else '') or provider.setup_error()))
                self._s_primary.setText('打开 AI 设置')
                self._s_primary.setVisible(True)
                self._s_second.setVisible(False)
                self._recheck_small.setVisible(True)
            self.state_changed.emit(state)
            return
        if state in ("ok", "checking"):
            self.setVisible(False)
        elif state == "out":
            self._small.setVisible(False)
            self._big.setVisible(True)
            self.setVisible(True)
        else:
            self._fill_small(state, error_text)
            self._big.setVisible(False)
            self._small.setVisible(True)
            self.setVisible(True)
        self.state_changed.emit(state)

    def _build_big(self) -> QWidget:
        card = Card("bg3", lambda: _mix_color("red", "l2", 0.40), 14)
        g = QHBoxLayout(card)
        g.setContentsMargins(24, 22, 24, 22)
        g.setSpacing(28)

        left = QVBoxLayout()
        left.setSpacing(0)
        head = row(Badge(20), Label("AI 还没登录", 17, 600, "t1"), None, spacing=8)
        left.addLayout(head)
        left.addSpacing(10)
        left.addWidget(rich("左边的 AI 用 reader <b>专用的</b> Claude 账号，跟平常用的分开。"
                            "登录一次就行，以后不用再管。", 13, "t2", line=1.75))
        left.addSpacing(16)
        left.addWidget(_Steps([
            "点「打开登录窗口」。会弹出一个 PowerShell，已经替你设好了，不用改任何东西。",
            "浏览器会打开 Claude 登录页：登<b>专用的账号</b>，别登成平常用的那个。",
            "登好了回到这里，会自己发现；没发现就点「重新检查」。",
        ]))
        left.addSpacing(18)
        self._open_btn = Button("打开登录窗口", "pri", icon_name="term")
        self._open_btn.clicked.connect(lambda: self._open_login())
        self._recheck_big = Button("重新检查", "ghost")
        self._recheck_big.clicked.connect(lambda: self.recheck())
        left.addLayout(row(self._open_btn, self._recheck_big, None))
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        self._copy_btn = Button("复制", size="sm", icon_name="copy")
        self._copy_btn.setToolTip("把这两行复制下来，贴到 PowerShell 里")
        self._copy_btn.clicked.connect(self._copy)
        right.addLayout(row(Label("想自己来：在 PowerShell 里依次运行这两行", 12, 400, "t3"), None,
                            self._copy_btn))
        code = Card("bg0", "l2", 8)
        cl = QVBoxLayout(code)
        cl.setContentsMargins(12, 10, 12, 10)
        code_text = Label(manual_commands(), 12, 600, "t1", selectable=True)
        f = code_text.font()
        f.setFamilies(["Cascadia Mono", "Consolas", theme.family()])
        f.setPixelSize(13)
        code_text.setFont(f)
        code_text.setStyleSheet(code_text.styleSheet())
        cl.addWidget(code_text)
        right.addWidget(code)
        right.addWidget(cap("这个文件夹里的登录跟平常的 ~/.claude 完全分开。"))
        right.addStretch(1)
        note = Card("bg4", None, 8)
        nl = QVBoxLayout(note)
        nl.setContentsMargins(12, 10, 12, 10)
        nl.addWidget(Label("不登录也能读书，只是左边问不了 AI、没有小节小测。", 13, 400, "t2", wrap=True))
        right.addWidget(note)

        g.addLayout(left, 1)
        g.addLayout(right, 1)
        return card

    def _build_small(self) -> QWidget:
        card = Card("bg3", "l2", 12)
        self._small_card = card
        v = QVBoxLayout(card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)
        self._s_icon = QStackedLayout()
        self._s_spin = IconLabel("spin", "t3", 16)
        self._s_ok = IconLabel("ok", "greent", 16)
        self._s_badge = Badge(18)
        for w in (self._s_spin, self._s_ok, self._s_badge):
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(w)
            self._s_icon.addWidget(holder)
        icon_box = QWidget()
        icon_box.setLayout(self._s_icon)
        icon_box.setFixedSize(20, 20)
        self._s_title = Label("", 15, 600, "t1")
        v.addLayout(row(icon_box, self._s_title, None, spacing=8))
        self._s_body = QLabel()
        self._s_body.setWordWrap(True)
        self._s_body.setTextFormat(Qt.RichText)
        self._s_body.setFont(theme.font(13, 400))
        v.addWidget(self._s_body)
        self._s_btns = QHBoxLayout()
        self._s_btns.setSpacing(8)
        self._s_primary = Button("", "pri", "sm")
        self._s_second = Button("", "", "sm")
        self._recheck_small = Button("重新检查", "ghost", "sm")
        self._recheck_small.clicked.connect(lambda: self.recheck())
        self._s_primary.clicked.connect(lambda: self._small_action(0))
        self._s_second.clicked.connect(lambda: self._small_action(1))
        for b in (self._s_primary, self._s_second, self._recheck_small):
            self._s_btns.addWidget(b)
        self._s_btns.addStretch(1)
        v.addSpacing(6)
        v.addLayout(self._s_btns)
        on_theme(card, lambda: self.state not in ("ok", "out", "checking") and self._fill_small(self.state))
        return card

    def _small_action(self, which: int) -> None:
        from reader.ai import provider
        if self._probe is probe and not provider.needs_login():
            self.settings_requested.emit()
            return
        s = self.state
        if s == "waiting":
            self._open_login()
        elif s == "ok_new":
            self._set_state("ok")
        elif s == "same":
            if which == 0:
                self._open_login(logout_first=True)
            else:
                self._accept_same()
        elif s == "error":
            self._set_state("out")

    def _fill_small(self, state: str, error_text: str = "") -> None:
        from reader.ai import provider
        if self._probe is probe and not provider.needs_login():
            self._s_icon.setCurrentIndex(2)
            self._s_title.setText(provider.label() + ' 需要设置')
            self._s_body.setText(escape(error_text or (self.result.error if self.result else '') or provider.setup_error()))
            self._s_primary.setText('打开 AI 设置')
            self._s_primary.setVisible(True)
            self._s_second.setVisible(False)
            self._recheck_small.setVisible(True)
            return
        r = self.result or AuthResult(False)
        email = f'<span style="font-variant-numeric: tabular-nums">{mask_email(r.email)}</span>'
        spec = {
            "waiting": (0, "等你在浏览器里登录",
                        "登录窗口开着。登好之后这里会自己变。关掉了也没关系，再点一次就行。",
                        ("", ""), ("再打开一次", "")),
            "ok_new": (1, "登好了",
                       f"专用账号 {email}，跟平常用的不是同一个。左边的 AI 可以用了。",
                       None, ("知道了", "ghost")),
            "same": (2, "登的是平常那个账号",
                     f"检测到 {email} 跟 ~/.claude 里是同一个账号。这样读书会跟平常的 Claude Code 抢同一份额度。",
                     ("退出，换个账号登", "pri"), ("就用这个", "ghost")),
            "error": (2, "查不了 AI 登录",
                      (error_text or self._error_text(r.error)),
                      None, ("看登录步骤", "")),
        }[state]
        idx, title, body, pri, sec = spec
        self._s_icon.setCurrentIndex(idx)
        self._s_title.setText(title)
        self._s_body.setText(f'<div style="line-height:170%">{body}</div>')
        self._s_body.setStyleSheet(f"QLabel {{ color: {theme.tok('t2')}; background: transparent; }}")
        if state == "waiting":
            self._s_primary.setVisible(False)
            self._s_second.setText(sec[0])
            self._s_second.set_kind(sec[1])
            self._s_second.setVisible(True)
            self._recheck_small.setVisible(True)
        else:
            self._s_primary.setVisible(bool(pri))
            if pri:
                self._s_primary.setText(pri[0])
            self._s_second.setVisible(True)
            self._s_second.setText(sec[0])
            self._s_second.set_kind(sec[1])
            self._recheck_small.setVisible(state == "error")
        self._small_card.border = (lambda: _mix_color("red", "l2", 0.40)) if state in ("same", "error") else "l2"
        self._small_card.update()

    @staticmethod
    def _error_text(err: str) -> str:
        if "找不到 claude" in (err or ""):
            return ("左边的 AI 要用 Claude Code 自带的 claude 命令，这台电脑上没找到。"
                    "装好 Claude Code 之后点「重新检查」。书照样能读。")
        return f"问 claude 登录状态时出了错：{err or '没有回应'}。书照样能读，过一会儿再点「重新检查」。"


def _mix_color(a: str, b: str, amount: float):
    from PySide6.QtGui import QColor
    return QColor(theme.mix(a, b, amount))


if __name__ == "__main__":
    # 演示：书架顶上的登录块。默认真的去查专用账号（只读，不改任何东西）；
    # 加 --fake out / same / waiting 看各状态。「打开登录窗口」在演示里只打印命令，不真开。
    import sys

    from PySide6.QtWidgets import QApplication

    theme.prepare_platform()
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.init_from_saved()
    fake = sys.argv[sys.argv.index("--fake") + 1] if "--fake" in sys.argv else ""
    fakes = {"out": AuthResult(False), "same": AuthResult(True, "me@example.com", "me@example.com"),
             "waiting": AuthResult(False)}
    w = QWidget()
    w.resize(900, 420)
    theme.style(w, "QWidget#host {{ background: {bg2}; }}")
    w.setObjectName("host")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(36, 28, 36, 28)
    banner = LoginBanner(prober=(lambda: fakes[fake]) if fake else probe,
                         launcher=lambda args: print("会运行：", " ".join(args[:4]), "…\n" + login_script()))
    status = cap("")
    banner.state_changed.connect(lambda s: status.setText(f"状态：{s} · {banner.status_line()[1]}"))
    lay.addWidget(banner)
    lay.addStretch(1)
    lay.addWidget(status)
    w.show()
    theme.titlebar(w)
    banner.recheck(quiet=True)
    if fake == "waiting":
        QTimer.singleShot(300, banner._open_login)
    sys.exit(app.exec())
