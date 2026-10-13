"""LockSession：把计时、会话状态、紧急出口、日报串起来的总控。

它**不碰任何窗口**：界面负责建窗口、连信号、把翻页 / 滚动 / 问 AI 转成 ping()，
把失去 / 得到激活转成 left() / returned()。这里只有逻辑，时间可注入，能完全离线单测。

信号（都是本模块的极简 Signal，不是 Qt）：
    remaining(int 秒)      剩余要读的秒数
    credited(int 秒)       已读秒数
    idle(bool)             进入 / 退出发呆
    left()                 失去激活
    returned()             回到前台
    finished(str 状态)     会话结束：finished / emergency_exit / forced_quit
    time_up()              读满计划时间（只在 allow_overtime=True 时发，见下）

驱动方式：界面用一个 QTimer 每秒调一次 tick()。tick 会推进计时、按需写心跳、发信号、
读满了自动 finish。计时本身按「真实读书时间」走，跟 tick 的频率无关。

每天的阅读统计走 StatusStore（status.json），路径见 state 模块常量。

## 时间到了不踢人（界面稿板 10①，allow_overtime=True）
默认（False）读满就自动 finish，跟以前一样。读书界面传 True：读满那一刻发 ``time_up``，
这次会话在磁盘上**当场**记为 finished、已计时间进今天的日报（之后被关掉也不会算成强行退出）；
然后接着计时 —— 主人点了「再读一会儿」，多读的照样进今天（每次心跳补一次，结束时补齐）。
多读阶段离开不算「离开」（已经读够了，随时可以走），只是暂停计时。结束由界面调 finish()。

## 发呆那 5 分钟
判定发呆时先扣下，主人回来（ping，包括点「在读」、翻页、滚动）就补回来；没人应就一直扣着。见 timer 模块。
"""

from __future__ import annotations

import time

from . import state as _state
from .emergency import EmergencyExit
from .signal import Signal
from .state import (
    STATUS_PATH, HEARTBEAT_INTERVAL, SESSION_STATE_PATH, SessionState, StatusStore,
    is_stale, mark_forced_quit, read_session_state,
)
from .timer import ReadingTimer


class LockSession:
    def __init__(self, *, clock=time.time, idle_timeout: float = 300.0,
                 emergency_required: int = 3, heartbeat_interval: float = HEARTBEAT_INTERVAL,
                 planned_default: int = 0,
                 state_path=SESSION_STATE_PATH, status_path=STATUS_PATH,
                 target_minutes: int = 45, allow_overtime: bool = False):
        self._clock = clock
        self._state_path = state_path
        self._heartbeat_interval = heartbeat_interval

        self.timer = ReadingTimer(idle_timeout=idle_timeout, clock=clock)
        self.emergency = EmergencyExit(required=emergency_required)
        self.status = StatusStore(status_path, clock=clock, target_minutes=target_minutes)

        self.session: SessionState | None = None
        self._last_heartbeat = 0.0
        self._left_at: float | None = None
        self._active = False
        self.allow_overtime = allow_overtime
        self._reached = False          # 已经读满计划时间（多读阶段）
        self._reported = 0.0           # 已经写进日报的秒数（多读阶段按差额补）
        self.last_away = 0.0           # 最近一次离开了多久（秒），界面写「刚才离开了 3 分 12 秒」

        # 信号
        self.remaining = Signal("remaining")
        self.credited = Signal("credited")
        self.idle = Signal("idle")
        self.left = Signal("left")
        self.returned = Signal("returned")
        self.finished = Signal("finished")
        self.time_up = Signal("time_up")

    # ── 生命周期 ─────────────────────────────────────────────
    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def start(self, minutes: int, now: float | None = None) -> SessionState:
        """开始一次读书。minutes = 这次打算读多久。"""
        now = self._now(now)
        self.session = SessionState.new(minutes, now)
        self.timer.start(now)
        self._active = True
        self._last_heartbeat = now
        self._left_at = None
        self._reached = False
        self._reported = 0.0
        self.last_away = 0.0
        self.emergency.cancel()
        self._write_state()
        self.status.bump("sessions")
        self._emit_progress()
        return self.session

    @property
    def active(self) -> bool:
        return self._active

    @property
    def reached(self) -> bool:
        """读满了计划时间，在多读阶段。"""
        return self._reached

    @property
    def extra_seconds(self) -> float:
        """多读了多少秒（没读满时是 0）。"""
        if self.session is None:
            return 0.0
        return max(0.0, self.timer.credited - self.session.planned_minutes * 60)

    def ping(self, now: float | None = None, credit_idle: bool = True) -> None:
        """界面在翻页 / 滚动 / 问 AI / 点「在读」时调。发呆中调它会恢复计时，
        并把判定发呆时扣下的那 5 分钟补回来（credit_idle=False 就不补）。"""
        if not self._active:
            return
        resumed = self.timer.ping(self._now(now), credit_idle=credit_idle)
        if resumed:
            self.idle.emit(False)
        self._emit_progress()

    def tick(self, now: float | None = None) -> None:
        """界面每秒调一次。推进计时、按需写心跳、读满自动结束。"""
        if not self._active:
            return
        now = self._now(now)
        event = self.timer.tick(now)
        if event == "idle":
            self.idle.emit(True)
        self._sync_credited()
        if now - self._last_heartbeat >= self._heartbeat_interval:
            self._last_heartbeat = now
            self.session.heartbeat = now
            self._write_state()
            if self._reached:
                self._flush_extra()
        self._emit_progress()
        if not self._reached and self.timer.remaining(self.session.planned_minutes) <= 0:
            if self.allow_overtime:
                self._reach(now)
            else:
                self.finish(now=now)

    def _reach(self, now: float) -> None:
        """读满计划时间：磁盘上记为读完、时间进日报，但不停表（多读的也算）。"""
        self._reached = True
        self.session.status = _state.FINISHED
        self.session.ended_at = now
        self._write_state()
        self._flush_extra()
        self.time_up.emit()

    def _flush_extra(self) -> None:
        delta = self.timer.credited - self._reported
        if delta > 0:
            self.status.add_credit(delta)
            self._reported = self.timer.credited

    def on_left(self, now: float | None = None) -> None:
        """应用失去激活。暂停计时，记下离开时刻。"""
        if not self._active or self.timer.left_now:
            return
        now = self._now(now)
        self.timer.left(now)
        self._left_at = now
        # 离开当下就计一次：离开后这次读书直接结束（时间到 / 强行关掉）也不会漏记。
        # 读满之后（多读阶段）随时可以走，不算「离开」，只暂停计时。
        if not self._reached:
            self.status.bump("leaves")
        self.left.emit()

    def on_returned(self, now: float | None = None) -> None:
        """回到前台。恢复计时（离开次数在 on_left 里已经记过）。"""
        if not self._active or not self.timer.left_now:
            return
        now = self._now(now)
        self.timer.returned(now)
        self.last_away = max(0.0, now - self._left_at) if self._left_at is not None else 0.0
        self._left_at = None
        self.returned.emit()
        self._emit_progress()

    def finish(self, now: float | None = None) -> None:
        """正常读完（或读满）。"""
        self._end("finished", _state.FINISHED, now)

    # ── 紧急出口 ─────────────────────────────────────────────
    def begin_emergency(self) -> str:
        return self.emergency.begin()

    def set_emergency_reason(self, reason: str | None) -> None:
        """紧急出口里选的原因（「有人找我」「要出门」……），退出时跟记录放在一起。"""
        self.emergency.set_reason(reason)

    def confirm_emergency(self, now: float | None = None,
                          reason: str | None = None) -> tuple[bool, str]:
        """确认一次紧急退出。返回 (是否已放行, 下一步或最后一步的 prompt_id)。

        reason 不为空就顺手记下原因（等于先调 set_emergency_reason）。"""
        if reason is not None:
            self.emergency.set_reason(reason)
        done = self.emergency.confirm()
        if done:
            self._end("emergency_exit", _state.EMERGENCY_EXIT, now)
        return done, self.emergency.current_prompt()

    def cancel_emergency(self) -> None:
        self.emergency.cancel()

    def _end(self, signal_status: str, state_status: str, now: float | None) -> None:
        if not self._active or self.session is None:
            return
        now = self._now(now)
        self.timer.stop(now)
        self._sync_credited()
        if self._reached:
            # 多读阶段结束：会话早记成读完了，只补多读的差额
            self.session.ended_at = now
            self._write_state()
            self._flush_extra()
        else:
            self.session.status = state_status
            self.session.ended_at = now
            if state_status == _state.EMERGENCY_EXIT:
                self.session.exit_reason = self.emergency.reason
            self._write_state()
            self.status.add_credit(self.timer.credited)
            if state_status == _state.EMERGENCY_EXIT:
                self.status.log_emergency(self.emergency.reason)
        self._active = False
        self.finished.emit(signal_status)

    # ── 启动时的强行退出恢复 ─────────────────────────────────
    def recover(self, now: float | None = None) -> SessionState | None:
        """启动时调用一次：上次若被强杀，会读到一个还是 active 但心跳早停的会话，记为强行退出。

        返回被恢复处理的那个会话（没有就 None）。已计时间保留到最后一次心跳。
        """
        now = self._now(now)
        prev = read_session_state(self._state_path)
        if prev is None:
            return None
        if not is_stale(prev, now):
            return None
        mark_forced_quit(prev)
        prev.write(self._state_path)
        self.status.add_credit(prev.credited_seconds)
        self.status.bump("forced_quits")
        return prev

    # ── 日报 ─────────────────────────────────────────────────
    def daily(self) -> dict:
        """今天的汇总，给界面看。"""
        s = self.status.today()
        return {
            "date": s.date,
            "credited_minutes": s.credited_minutes,
            "credited_seconds": int(s.credited_seconds),
            "target_minutes": s.target_minutes,
            "sessions": s.sessions,
            "leaves": s.leaves,
            "emergency_exits": s.emergency_exits,
            "forced_quits": s.forced_quits,
            "emergency_log": list(s.emergency_log),
        }

    # ── 内部 ─────────────────────────────────────────────────
    def _sync_credited(self) -> None:
        if self.session is not None:
            self.session.credited_seconds = self.timer.credited

    def _write_state(self) -> None:
        if self.session is not None:
            self._sync_credited()
            self.session.write(self._state_path)

    def _emit_progress(self) -> None:
        if self.session is None:
            return
        self.credited.emit(int(self.timer.credited))
        self.remaining.emit(int(self.timer.remaining(self.session.planned_minutes)))
