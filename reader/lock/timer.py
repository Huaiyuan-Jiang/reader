"""读书计时器：只算真正在读的时间，发呆和离开都不算。

计时规则：
- 正常读书时才累加「已计时间」。
- **发呆**：连续 ``idle_timeout``（默认 5 分钟）没有活动 ping（翻页 / 滚动 / 问 AI 由界面调 ping()），
  就暂停计时，发一次 idle 信号让界面轻声问一句。任意 ping 立刻恢复。
- **离开**：应用失去激活（切到别的窗口）时 left()，也暂停；returned() 恢复，并把发呆计时清零重来
  （人刚回来不该立刻又被判发呆）。
- **发呆那 5 分钟先扣、回来补**（2026-09-24 定）：判定发呆的那一刻，把「最后一次活动 → 判定发呆」
  这段已经计进去的时间先扣下来（``withheld``）。主人回来接着读（点「在读」、翻页、滚动 —— 都走 ping()），
  就原样补回；一直没人应（离开了、紧急退出了）就不补，算扣掉。

时间可注入：构造时传 ``clock``，或每个方法传 ``now``，单测里手搓时间线，一秒都不用真等。
本模块不碰 Qt、不碰系统，纯算术。
"""

from __future__ import annotations

import time
from datetime import datetime, time as dt_time, timedelta


class ReadingTimer:
    def __init__(self, idle_timeout: float = 300.0, clock=time.time):
        self.idle_timeout = float(idle_timeout)
        self._clock = clock
        self.credited = 0.0
        self._running = False
        self._left = False
        self._idle = False
        self._last_ping = 0.0
        self._mark = 0.0     # 上次结算的时刻
        self._credited_at_ping = 0.0   # 最后一次活动时已计的秒数
        self.withheld = 0.0  # 发呆时先扣下、等主人回来补的秒数
        self.elapsed = 0.0   # 开始以来实际过去的时间（算发呆扣了多少用）
        self.away = 0.0      # 其中离开的时间
        self._wall_mark = 0.0
        self._credit_by_day: dict[str, float] = {}
        self._credit_by_day_at_ping: dict[str, float] = {}
        self._withheld_by_day: dict[str, float] = {}

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    @property
    def crediting(self) -> bool:
        """此刻是否在累加时间。"""
        return self._running and not self._left and not self._idle

    @property
    def idle(self) -> bool:
        return self._idle

    @property
    def left_now(self) -> bool:
        return self._left

    @property
    def idle_lost(self) -> float:
        """因为发呆没算进去的秒数（= 过去的时间 - 离开的时间 - 已计时间）。"""
        return max(0.0, self.elapsed - self.away - self.credited)

    @property
    def credited_by_day(self) -> dict[str, float]:
        """Current credited seconds split by the local calendar day they accrued on."""
        return dict(self._credit_by_day)

    @staticmethod
    def _day_key(wall: float) -> str:
        return datetime.fromtimestamp(wall).strftime("%Y-%m-%d")

    def _add_wall_interval(self, wall_start: float, seconds: float, sign: float = 1.0) -> dict[str, float]:
        """Split an elapsed interval at local midnight and apply it to the day ledger."""
        added: dict[str, float] = {}
        cursor = wall_start
        remaining = max(0.0, seconds)
        while remaining > 1e-9:
            key = self._day_key(cursor)
            local_day = datetime.fromtimestamp(cursor).date()
            try:
                next_day = datetime.combine(local_day + timedelta(days=1), dt_time.min).timestamp()
            except (OSError, OverflowError, ValueError):
                self._credit_by_day[key] = max(0.0, self._credit_by_day.get(key, 0.0) + sign * remaining)
                added[key] = added.get(key, 0.0) + sign * remaining
                break
            span = min(remaining, max(0.0, next_day - cursor))
            if span <= 1e-9:  # Guard unusual timezone transitions from a zero-length loop.
                span = remaining
            value = sign * span
            self._credit_by_day[key] = max(0.0, self._credit_by_day.get(key, 0.0) + value)
            added[key] = added.get(key, 0.0) + value
            cursor += span
            remaining -= span
        return added

    def _accrue(self, now: float, elapsed_now: float | None = None) -> None:
        # 只把「上次结算到现在」这段里真正在读的时间加进去。关键是别把发呆那段也算上：
        # tick 可能隔了好几秒才来，发现发呆时已经晚了，所以结算的终点最多到「最后一次活动 + 发呆阈值」，
        # 越过这条线的时间一律不计（那就是发呆）。
        elapsed_now = now if elapsed_now is None else elapsed_now
        if elapsed_now < self._mark:
            # A custom/wall clock moved backward. Production uses monotonic time,
            # while injected clocks re-anchor activity here so idle detection recovers.
            self._mark = elapsed_now
            self._wall_mark = now
            self._last_ping = elapsed_now
            self._credited_at_ping = self.credited
            self._credit_by_day_at_ping = dict(self._credit_by_day)
            return
        dt = max(0.0, elapsed_now - self._mark)
        if self.crediting:
            end = elapsed_now
            idle_limit = self._last_ping + self.idle_timeout
            if end > idle_limit:
                end = idle_limit
            credit = max(0.0, end - self._mark)
            if credit:
                self.credited += credit
                self._add_wall_interval(self._wall_mark, credit)
        if self._running:
            self.elapsed += dt
            if self._left:
                self.away += dt
        self._mark = elapsed_now
        self._wall_mark = now

    def start(self, now: float | None = None, *, elapsed_now: float | None = None) -> None:
        now = self._now(now)
        elapsed_now = now if elapsed_now is None else elapsed_now
        self.credited = 0.0
        self._running = True
        self._left = False
        self._idle = False
        self._last_ping = elapsed_now
        self._mark = elapsed_now
        self._wall_mark = now
        self._credited_at_ping = 0.0
        self.withheld = 0.0
        self.elapsed = 0.0
        self.away = 0.0
        self._credit_by_day = {}
        self._credit_by_day_at_ping = {}
        self._withheld_by_day = {}

    def stop(self, now: float | None = None, *, elapsed_now: float | None = None) -> None:
        now = self._now(now)
        self._accrue(now, elapsed_now)
        self._running = False

    def tick(self, now: float | None = None, *, elapsed_now: float | None = None) -> str | None:
        """推进时间。刚跨进发呆时返回 "idle"，否则 None。界面按秒或按需调用它。"""
        now = self._now(now)
        elapsed_now = now if elapsed_now is None else elapsed_now
        self._accrue(now, elapsed_now)
        if self._running and not self._left and not self._idle:
            if (elapsed_now - self._last_ping) >= self.idle_timeout:
                self._idle = True
                # 最后一次活动之后计进去的那段（通常正好 idle_timeout）先扣下，主人回来再补
                self.withheld = max(0.0, self.credited - self._credited_at_ping)
                self.credited -= self.withheld
                self._withheld_by_day = {
                    key: max(0.0, value - self._credit_by_day_at_ping.get(key, 0.0))
                    for key, value in self._credit_by_day.items()
                }
                for key, value in self._withheld_by_day.items():
                    self._credit_by_day[key] = max(0.0, self._credit_by_day.get(key, 0.0) - value)
                return "idle"
        return None

    def ping(self, now: float | None = None, credit_idle: bool = True, *,
             elapsed_now: float | None = None) -> bool:
        """一次活动（翻页 / 滚动 / 问 AI / 点「在读」）。若因此从发呆里恢复，返回 True。

        从发呆里恢复时，credit_idle=True 就把判定发呆时扣下的那段补回来。
        """
        now = self._now(now)
        elapsed_now = now if elapsed_now is None else elapsed_now
        self._accrue(now, elapsed_now)
        self._last_ping = elapsed_now
        resumed = self._idle
        if resumed:
            if credit_idle:
                self.credited += self.withheld
                for key, value in self._withheld_by_day.items():
                    self._credit_by_day[key] = self._credit_by_day.get(key, 0.0) + value
            self.withheld = 0.0
        self._withheld_by_day = {}
        self._idle = False
        self._mark = elapsed_now
        self._wall_mark = now
        self._credited_at_ping = self.credited
        self._credit_by_day_at_ping = dict(self._credit_by_day)
        return resumed

    def left(self, now: float | None = None, *, elapsed_now: float | None = None) -> None:
        now = self._now(now)
        self._accrue(now, elapsed_now)
        self._left = True

    def returned(self, now: float | None = None, *, elapsed_now: float | None = None) -> None:
        now = self._now(now)
        elapsed_now = now if elapsed_now is None else elapsed_now
        self._accrue(now, elapsed_now)
        self._left = False
        self._last_ping = elapsed_now      # 刚回来，重新开始数发呆
        if self._idle:
            self.withheld = 0.0    # 发呆没人应、接着又离开了：那段就算扣掉
            self._withheld_by_day = {}
        self._idle = False
        self._mark = elapsed_now
        self._wall_mark = now
        self._credited_at_ping = self.credited
        self._credit_by_day_at_ping = dict(self._credit_by_day)

    def remaining(self, planned_minutes: int) -> float:
        return max(0.0, planned_minutes * 60 - self.credited)
