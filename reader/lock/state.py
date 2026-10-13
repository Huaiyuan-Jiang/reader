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
import shutil
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


def _backup_bad_json(path: Path) -> None:
    """Preserve an unreadable or non-object JSON file before a later write replaces it."""
    path = Path(path)
    try:
        if not path.is_file():
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(f"{path.name}.bad-{stamp}")
        shutil.copy2(path, backup)
    except OSError:
        # Keep reads resilient when a damaged file is also inaccessible to copy.
        pass


def read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            value = json.load(f)
        if isinstance(value, dict):
            return value
        _backup_bad_json(path)
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        _backup_bad_json(path)
    except OSError:
        return None
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
    pid: int | None = None
    credited_by_day: dict[str, float] = field(default_factory=dict)

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


def process_is_alive(pid: int) -> bool:
    """Check whether a recorded owner process exists without sending it a signal."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        handle = open_process(0x1000 | 0x00100000, 0, int(pid))
        if not handle:
            # Access denied is safer to treat as live: never recover another process's session.
            return ctypes.get_last_error() == 5
        try:
            return wait(handle, 0) != 0  # WAIT_OBJECT_0 means signaled/dead; unknown is treated as live.
        finally:
            close(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_stale(state: SessionState, now: float, stale_after: float = STALE_AFTER) -> bool:
    """An active session whose heartbeat is older than the legacy timeout."""
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

    def _day(self, data: dict, key: str) -> dict:
        day = data.setdefault("days", {}).get(key)
        if day is None:
            day = asdict(DailySummary(date=key, target_minutes=self.target_minutes))
            data["days"][key] = day
        day.setdefault("target_minutes", self.target_minutes)
        return day

    def _today(self, data: dict) -> dict:
        return self._day(data, _day_key(self._clock()))

    def _commit(self, mutate, *, at: float | None = None,
                day_key: str | None = None) -> DailySummary:
        data = self._load()
        key = day_key or _day_key(self._clock() if at is None else at)
        day = self._day(data, key)
        mutate(day)
        day["updated_at"] = self._clock()
        today_key = _day_key(self._clock())
        today = data.setdefault("days", {}).get(today_key)
        if today is not None:
            data["today"] = today
        self._save(data)
        return DailySummary(**{k: v for k, v in day.items() if k in DailySummary.__dataclass_fields__})

    def add_credit(self, seconds: float, *, at: float | None = None,
                   day_key: str | None = None) -> DailySummary:
        def mutate(day):
            day["credited_seconds"] = max(0.0, day["credited_seconds"] + seconds)
        return self._commit(mutate, at=at, day_key=day_key)

    def bump(self, field: str, by: int = 1, *, at: float | None = None,
             day_key: str | None = None) -> DailySummary:
        def m(d):
            d[field] = d.get(field, 0) + by
        return self._commit(m, at=at, day_key=day_key)

    def log_emergency(self, reason: str | None) -> DailySummary:
        """记一次紧急退出（次数 + 时间 + 原因）。"""
        now = self._clock()
        entry = {"at": now, "time": datetime.fromtimestamp(now).strftime("%H:%M"), "reason": reason}

        def m(d):
            d["emergency_exits"] = d.get("emergency_exits", 0) + 1
            d.setdefault("emergency_log", []).append(entry)
        return self._commit(m, at=now)

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
