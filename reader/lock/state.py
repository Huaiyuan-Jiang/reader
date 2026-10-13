"""会话状态和每天的阅读统计，都是本地 JSON，原子写。

两个文件（路径见下方常量，都在 ``%LOCALAPPDATA%\\reader\\``，设了环境变量 READER_DATA_DIR 就在那里）：

- ``session.json``（SESSION_STATE_PATH）：当前这次读书的实时状态，每 ~15 秒写一次心跳。
  程序被强杀后，下次启动读这个文件：如果它还是 active 但心跳早就停了，就判定为「强行退出」，
  已计的时间保留到最后一次心跳为止（心跳里存的 credited_seconds 就是那个时刻的值）。
- ``status.json``（STATUS_PATH）：每天的阅读统计 —— 今天读了几分钟、几次、离开几次、
  紧急退出 / 强行退出各几次。按日期分桶。书架读它显示今天和这一周。

原子写：先写同目录下的临时文件，fsync，再 os.replace 覆盖过去。os.replace 在 Windows 同盘符上是原子的，
中途断电最坏只丢这一次写入，不会读到写了一半的半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


# ── 路径 ─────────────────────────────────────────────────────
def _base_dir() -> Path:
    """跟书架、识别结果同一个位置：环境变量 READER_DATA_DIR 可以整个换掉（测试、开发时不碰真的统计）。"""
    if os.environ.get("READER_DATA_DIR"):
        return Path(os.environ["READER_DATA_DIR"])
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(root) / "reader"


STATE_DIR = _base_dir()
SESSION_STATE_PATH = STATE_DIR / "session.json"      # 当前会话 + 心跳
STATUS_PATH = STATE_DIR / "status.json"              # 每天的阅读统计

HEARTBEAT_INTERVAL = 15.0        # 心跳间隔（秒）
STALE_AFTER = HEARTBEAT_INTERVAL * 3   # 超过这么久没心跳，判定进程已死

# 状态取值
ACTIVE = "active"
FINISHED = "finished"
EMERGENCY_EXIT = "emergency_exit"
FORCED_QUIT = "forced_quit"


# ── 原子写 / 读 ──────────────────────────────────────────────
def atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ── 会话状态 ─────────────────────────────────────────────────
@dataclass
class SessionState:
    session_id: str
    planned_minutes: int
    credited_seconds: float = 0.0
    status: str = ACTIVE
    started_at: float = 0.0
    heartbeat: float = 0.0
    ended_at: float | None = None
    exit_reason: str | None = None     # 紧急退出时选的原因

    @staticmethod
    def new(planned_minutes: int, now: float) -> "SessionState":
        return SessionState(
            session_id=uuid.uuid4().hex,
            planned_minutes=int(planned_minutes),
            credited_seconds=0.0,
            status=ACTIVE,
            started_at=now,
            heartbeat=now,
            ended_at=None,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SessionState":
        known = {f: d[f] for f in SessionState.__dataclass_fields__ if f in d}
        return SessionState(**known)

    def write(self, path: Path = SESSION_STATE_PATH) -> None:
        atomic_write_json(path, self.to_dict())


def read_session_state(path: Path = SESSION_STATE_PATH) -> SessionState | None:
    d = read_json(path)
    if not d or "session_id" not in d:
        return None
    try:
        return SessionState.from_dict(d)
    except TypeError:
        return None


def is_stale(state: SessionState, now: float, stale_after: float = STALE_AFTER) -> bool:
    """active 但心跳老旧 —— 说明进程已经不在了。"""
    return state.status == ACTIVE and (now - state.heartbeat) > stale_after


def mark_forced_quit(state: SessionState) -> SessionState:
    """把一次残留的 active 会话定为强行退出：结束时间取最后一次心跳，已计时间原样保留。"""
    state.status = FORCED_QUIT
    state.ended_at = state.heartbeat
    return state


# ── 每天的阅读统计 ──────────────────────────────────────────
def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


@dataclass
class DailySummary:
    date: str
    credited_seconds: float = 0.0
    sessions: int = 0
    leaves: int = 0
    emergency_exits: int = 0
    forced_quits: int = 0
    target_minutes: int = 45
    updated_at: float = 0.0
    # 每次紧急退出一条：{"at": 时间戳, "time": "14:32", "reason": "要出门"}
    emergency_log: list = field(default_factory=list)

    @property
    def credited_minutes(self) -> int:
        return int(self.credited_seconds // 60)


class StatusStore:
    """阅读统计的读写。所有计数都累加到「今天」那一桶，历史桶保留在同一个文件里。"""

    def __init__(self, path: Path = STATUS_PATH, *, clock=time.time,
                 target_minutes: int = 45):
        self.path = Path(path)
        self._clock = clock
        self.target_minutes = target_minutes

    def _load(self) -> dict:
        return read_json(self.path) or {"days": {}}

    def _save(self, data: dict) -> None:
        atomic_write_json(self.path, data)

    def _today(self, data: dict) -> dict:
        key = _day_key(self._clock())
        day = data.setdefault("days", {}).get(key)
        if day is None:
            day = asdict(DailySummary(date=key, target_minutes=self.target_minutes))
            data["days"][key] = day
        day.setdefault("target_minutes", self.target_minutes)
        return day

    def _commit(self, mutate) -> DailySummary:
        data = self._load()
        day = self._today(data)
        mutate(day)
        day["updated_at"] = self._clock()
        data["today"] = day  # 顶层放一份今天的，读的时候不用按日期找
        self._save(data)
        return DailySummary(**{k: v for k, v in day.items() if k in DailySummary.__dataclass_fields__})

    def add_credit(self, seconds: float) -> DailySummary:
        return self._commit(lambda d: d.update(credited_seconds=d["credited_seconds"] + max(0.0, seconds)))

    def bump(self, field: str, by: int = 1) -> DailySummary:
        def m(d):
            d[field] = d.get(field, 0) + by
        return self._commit(m)

    def log_emergency(self, reason: str | None) -> DailySummary:
        """记一次紧急退出（次数 + 时间 + 原因）。"""
        now = self._clock()
        entry = {"at": now, "time": datetime.fromtimestamp(now).strftime("%H:%M"), "reason": reason}

        def m(d):
            d["emergency_exits"] = d.get("emergency_exits", 0) + 1
            d.setdefault("emergency_log", []).append(entry)
        return self._commit(m)

    def week_total(self, field_name: str) -> int | float:
        """这周（周一起）某个计数的合计，比如这周紧急退出几次。"""
        today = datetime.fromtimestamp(self._clock()).date()
        monday = today.toordinal() - today.weekday()
        total = 0
        for key, day in (self._load().get("days") or {}).items():
            try:
                d = datetime.strptime(key, "%Y-%m-%d").date().toordinal()
            except ValueError:
                continue
            if monday <= d <= monday + 6:
                total += day.get(field_name, 0) or 0
        return total

    def today(self) -> DailySummary:
        data = self._load()
        day = self._today(data)
        known = {k: v for k, v in day.items() if k in DailySummary.__dataclass_fields__}
        return DailySummary(**known)
