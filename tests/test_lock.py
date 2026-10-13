"""锁屏核心的离线单测。

时间全部注入（假时钟），JSON 写到临时目录，不建任何窗口、不碰系统。
跑法：
    python -m pytest -q            # 有 pytest
    python -m unittest -q tests.test_lock   # 没 pytest 也行（本文件是标准 unittest）
"""

import tempfile
import unittest
from pathlib import Path

from reader.lock import state as st
from reader.lock.emergency import EmergencyExit
from reader.lock.session import LockSession
from reader.lock.state import SessionState, StatusStore
from reader.lock.timer import ReadingTimer


class Clock:
    """可手动推进的假时钟。"""

    def __init__(self, t=1000.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt
        return self.t


# ── 计时器 ───────────────────────────────────────────────────
class TestReadingTimer(unittest.TestCase):
    def test_credits_only_while_reading(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(60)
        t.tick()
        self.assertAlmostEqual(t.credited, 60, places=3)

    def test_idle_pauses_after_timeout(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(299)
        self.assertIsNone(t.tick())          # 还没到 5 分钟
        self.assertFalse(t.idle)
        c.advance(2)                          # 301 秒无 ping
        self.assertEqual(t.tick(), "idle")    # 跨进发呆
        self.assertTrue(t.idle)
        credited_at_idle = t.credited
        c.advance(1000)                       # 发呆期间不计时
        t.tick()
        self.assertAlmostEqual(t.credited, credited_at_idle, places=3)

    def test_ping_resumes_from_idle(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(400)
        t.tick()
        self.assertTrue(t.idle)
        resumed = t.ping()
        self.assertTrue(resumed)
        self.assertFalse(t.idle)
        c.advance(30)
        t.tick()
        self.assertAlmostEqual(t.credited, 300 + 30, places=3)  # 发呆前 300 + 恢复后 30

    def test_left_pauses_and_return_resets_idle_window(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(60)
        t.left()
        base = t.credited
        c.advance(1000)                       # 离开期间不计时
        t.tick()
        self.assertAlmostEqual(t.credited, base, places=3)
        t.returned()
        c.advance(299)                        # 刚回来，发呆窗口重新计时
        self.assertIsNone(t.tick())           # 还没到发呆阈值
        t.ping()                              # 有活动，接着读
        c.advance(60)
        t.tick()
        self.assertAlmostEqual(t.credited, base + 359, places=3)


# ── 紧急出口 ─────────────────────────────────────────────────
class TestEmergencyExit(unittest.TestCase):
    def test_needs_n_consecutive(self):
        ee = EmergencyExit(required=3)
        ee.begin()
        self.assertFalse(ee.confirm())
        self.assertFalse(ee.confirm())
        self.assertTrue(ee.confirm())
        self.assertTrue(ee.done)

    def test_cancel_resets(self):
        ee = EmergencyExit(required=3)
        ee.begin()
        ee.confirm()
        ee.confirm()
        ee.cancel()
        self.assertEqual(ee.remaining(), 3)
        self.assertFalse(ee.confirm())        # 从头再来

    def test_prompt_id_differs_each_step(self):
        ee = EmergencyExit(required=3)
        seen = []
        ee.begin()
        seen.append(ee.current_prompt())
        ee.confirm(); seen.append(ee.current_prompt())
        ee.confirm(); seen.append(ee.current_prompt())
        self.assertEqual(len(set(seen)), 3)   # 三步措辞都不同


# ── 会话状态 / 强行退出检测 ─────────────────────────────────
class TestSessionStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_roundtrip(self):
        s = SessionState.new(30, now=1000.0)
        s.credited_seconds = 123.0
        s.write(self.path)
        back = st.read_session_state(self.path)
        self.assertEqual(back.session_id, s.session_id)
        self.assertEqual(back.credited_seconds, 123.0)
        self.assertEqual(back.status, st.ACTIVE)

    def test_stale_active_is_forced_quit(self):
        s = SessionState.new(30, now=1000.0)
        s.credited_seconds = 600.0
        s.heartbeat = 1000.0
        s.write(self.path)
        # 90 秒后启动，心跳早过期（阈值 45 秒）
        loaded = st.read_session_state(self.path)
        self.assertTrue(st.is_stale(loaded, now=1090.0))
        st.mark_forced_quit(loaded)
        self.assertEqual(loaded.status, st.FORCED_QUIT)
        self.assertEqual(loaded.ended_at, 1000.0)     # 结束时间取最后心跳
        self.assertEqual(loaded.credited_seconds, 600.0)  # 已计时间保留

    def test_fresh_active_not_stale(self):
        s = SessionState.new(30, now=1000.0)
        s.heartbeat = 1000.0
        self.assertFalse(st.is_stale(s, now=1010.0))


# ── 日报 ─────────────────────────────────────────────────────
class TestStatusStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "status.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_accumulates_today(self):
        c = Clock(1_700_000_000.0)
        store = StatusStore(self.path, clock=c, target_minutes=45)
        store.bump("sessions")
        store.add_credit(600)           # 10 分钟
        store.bump("leaves")
        store.bump("forced_quits")
        today = store.today()
        self.assertEqual(today.sessions, 1)
        self.assertEqual(today.credited_minutes, 10)
        self.assertEqual(today.leaves, 1)
        self.assertEqual(today.forced_quits, 1)
        self.assertEqual(today.target_minutes, 45)


# ── 控制器整合 ───────────────────────────────────────────────
class TestLockSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "session.json"
        self.status_path = Path(self.tmp.name) / "status.json"
        self.c = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, **kw):
        return LockSession(clock=self.c, state_path=self.state_path,
                           status_path=self.status_path, **kw)

    def test_start_and_finish_credits_daily(self):
        s = self._mk(idle_timeout=300)
        fired = []
        s.finished.connect(lambda status: fired.append(status))
        s.start(minutes=1)
        for _ in range(60):
            self.c.advance(1)
            s.tick()
        # 60 秒 = 计划 1 分钟，读满自动结束
        self.assertEqual(fired, ["finished"])
        self.assertGreaterEqual(s.daily()["credited_minutes"], 1)
        self.assertEqual(s.daily()["sessions"], 1)

    def test_idle_signal_and_resume(self):
        s = self._mk(idle_timeout=300)
        idle_events = []
        s.idle.connect(lambda v: idle_events.append(v))
        s.start(minutes=60)
        self.c.advance(301)
        s.tick()
        self.assertIn(True, idle_events)
        s.ping()
        self.assertIn(False, idle_events)

    def test_leave_records_and_pauses(self):
        s = self._mk(idle_timeout=300)
        left, ret = [], []
        s.left.connect(lambda: left.append(1))
        s.returned.connect(lambda: ret.append(1))
        s.start(minutes=60)
        self.c.advance(30)
        s.tick()
        s.on_left()
        self.c.advance(100)          # 离开期间不计时
        s.on_returned()
        self.assertEqual(len(left), 1)
        self.assertEqual(len(ret), 1)
        self.assertEqual(s.daily()["leaves"], 1)
        self.assertLess(s.timer.credited, 40)   # 那 100 秒没被算进去

    def test_leave_counted_even_without_return(self):
        # 2026-09-24 真机检查发现：离开后没回来就结束，离开次数是 0
        s = self._mk(idle_timeout=300)
        s.start(minutes=60)
        self.c.advance(10)
        s.on_left()
        self.c.advance(10)
        s.finish()
        self.assertEqual(s.daily()["leaves"], 1)

    def test_emergency_exit_records(self):
        s = self._mk(emergency_required=3)
        fired = []
        s.finished.connect(lambda status: fired.append(status))
        s.start(minutes=60)
        p0 = s.begin_emergency()
        done, p1 = s.confirm_emergency()
        self.assertFalse(done)
        self.assertNotEqual(p0, p1)
        self.assertFalse(s.confirm_emergency()[0])
        done, _ = s.confirm_emergency()
        self.assertTrue(done)
        self.assertEqual(fired, ["emergency_exit"])
        self.assertEqual(s.daily()["emergency_exits"], 1)

    def test_recover_forced_quit(self):
        # 先造一个「还是 active、心跳很旧」的残留会话
        prev = SessionState.new(45, now=1000.0)
        prev.credited_seconds = 600.0
        prev.heartbeat = 1000.0
        prev.write(self.state_path)
        self.c.t = 2000.0            # 很久以后才启动
        s = self._mk()
        rec = s.recover()
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, st.FORCED_QUIT)
        self.assertEqual(s.daily()["forced_quits"], 1)
        self.assertEqual(s.daily()["credited_minutes"], 10)   # 保留到最后心跳的 10 分钟

    def test_heartbeat_written_during_tick(self):
        s = self._mk(heartbeat_interval=15)
        s.start(minutes=60)
        for _ in range(20):
            self.c.advance(1)
            s.tick()
        on_disk = st.read_session_state(self.state_path)
        self.assertIsNotNone(on_disk)
        self.assertGreaterEqual(on_disk.heartbeat, 1015.0)   # 心跳更新过
        self.assertEqual(on_disk.status, st.ACTIVE)


if __name__ == "__main__":
    unittest.main()
