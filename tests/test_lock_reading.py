"""读书界面（D）给锁屏核心加的几样：发呆补回、多读、紧急退出原因、离开时长。

纯逻辑，假时钟，不建窗口。跑法：python -m unittest discover -s tests
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from reader.lock import state as st
from reader.lock.session import LockSession
from reader.lock.state import StatusStore
from reader.lock.timer import ReadingTimer

from tests.test_lock import Clock


class TestIdleCreditBack(unittest.TestCase):
    """发呆弹出后点了「在读」就把那 5 分钟补回来；一直没人理就全扣。"""

    def test_idle_withholds_then_ping_credits_back(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(100)
        t.ping()                      # 100 秒时翻了一页
        c.advance(301)
        self.assertEqual(t.tick(), "idle")
        self.assertAlmostEqual(t.credited, 100, places=3)     # 那 5 分钟先扣下
        self.assertAlmostEqual(t.withheld, 300, places=3)
        c.advance(600)                # 发呆期间照样不算
        t.tick()
        self.assertAlmostEqual(t.credited, 100, places=3)
        self.assertTrue(t.ping())     # 点「在读」
        self.assertAlmostEqual(t.credited, 400, places=3)     # 补回来了
        self.assertEqual(t.withheld, 0)
        self.assertAlmostEqual(t.idle_lost, 601, places=3)    # 只扣了发呆之后那段

    def test_no_credit_back_when_asked_not_to(self):
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(301)
        t.tick()
        t.ping(credit_idle=False)
        self.assertAlmostEqual(t.credited, 0, places=3)

    def test_idle_then_leave_keeps_deduction(self):
        # 发呆没人应，接着切走了：那 5 分钟不补
        c = Clock()
        t = ReadingTimer(idle_timeout=300, clock=c)
        t.start()
        c.advance(310)
        t.tick()
        t.left()
        c.advance(50)
        t.returned()
        self.assertAlmostEqual(t.credited, 0, places=3)
        self.assertFalse(t.idle)
        c.advance(20)
        t.ping()                       # 回来后的活动不再补那段
        self.assertAlmostEqual(t.credited, 20, places=3)
        self.assertAlmostEqual(t.away, 50, places=3)

    def test_session_ping_resumes_and_credits(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = Clock()
        s = LockSession(clock=c, idle_timeout=300, state_path=Path(tmp.name) / "s.json",
                        status_path=Path(tmp.name) / "d.json")
        idle = []
        s.idle.connect(idle.append)
        rem = []
        s.remaining.connect(rem.append)
        s.start(minutes=30)
        c.advance(305)
        s.tick()
        self.assertEqual(idle, [True])
        self.assertEqual(rem[-1], 30 * 60)       # 扣下后剩余回到 30 分钟
        s.ping()
        self.assertEqual(idle, [True, False])
        self.assertEqual(rem[-1], 30 * 60 - 300)  # 补回来了


class TestOvertime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "session.json"
        self.status_path = Path(self.tmp.name) / "status.json"
        self.c = Clock(1_700_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def _mk(self, **kw):
        return LockSession(clock=self.c, state_path=self.state_path, status_path=self.status_path,
                           heartbeat_interval=15, allow_overtime=True, **kw)

    def test_time_up_does_not_finish_and_extra_counts(self):
        s = self._mk()
        ups, fin = [], []
        s.time_up.connect(lambda: ups.append(1))
        s.finished.connect(fin.append)
        s.start(minutes=1)
        for _ in range(60):
            self.c.advance(1)
            s.tick()
        self.assertEqual(ups, [1])
        self.assertEqual(fin, [])
        self.assertTrue(s.reached and s.active)
        # 时间到的那一刻已经记为读完，进了日报
        self.assertEqual(st.read_session_state(self.state_path).status, st.FINISHED)
        self.assertEqual(s.daily()["credited_seconds"], 60)
        for _ in range(90):                # 再读 90 秒
            self.c.advance(1)
            s.tick()
        self.assertEqual(ups, [1])         # 只发一次
        self.assertAlmostEqual(s.extra_seconds, 90, places=3)
        s.finish()
        self.assertEqual(fin, ["finished"])
        self.assertEqual(s.daily()["credited_seconds"], 150)   # 多读的算进今天，不重复算

    def test_overtime_leave_not_counted(self):
        s = self._mk()
        s.start(minutes=1)
        self.c.advance(61)
        s.tick()
        s.on_left()
        self.c.advance(30)
        s.on_returned()
        self.assertEqual(s.daily()["leaves"], 0)

    def test_killed_during_overtime_is_not_forced_quit(self):
        s = self._mk()
        s.start(minutes=1)
        for _ in range(100):
            self.c.advance(1)
            s.tick()
        # 进程被杀：很久以后再启动
        self.c.advance(3600)
        s2 = self._mk()
        self.assertIsNone(s2.recover())
        self.assertEqual(s2.daily()["forced_quits"], 0)
        self.assertGreaterEqual(s2.daily()["credited_seconds"], 90)   # 心跳时补过多读的

    def test_default_still_auto_finishes(self):
        s = LockSession(clock=self.c, state_path=self.state_path, status_path=self.status_path)
        fin = []
        s.finished.connect(fin.append)
        s.start(minutes=1)
        self.c.advance(61)
        s.tick()
        self.assertEqual(fin, ["finished"])


class TestEmergencyReason(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "session.json"
        self.status_path = Path(self.tmp.name) / "status.json"
        self.c = Clock(1_700_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_four_steps_with_reason_recorded(self):
        s = LockSession(clock=self.c, state_path=self.state_path, status_path=self.status_path,
                        emergency_required=4, idle_timeout=10_000)
        fin = []
        s.finished.connect(fin.append)
        s.start(minutes=30)
        self.c.advance(600)
        s.tick()
        s.begin_emergency()
        self.assertFalse(s.confirm_emergency()[0])                        # 第 1 步：要走
        self.assertFalse(s.confirm_emergency(reason="要出门")[0])   # 第 2 步：原因
        self.assertFalse(s.confirm_emergency()[0])                        # 第 3 步：等 5 秒后确定
        self.assertTrue(s.confirm_emergency()[0])                         # 第 4 步：最后确认
        self.assertEqual(fin, ["emergency_exit"])
        d = s.daily()
        self.assertEqual(d["emergency_exits"], 1)
        self.assertEqual(d["emergency_log"][0]["reason"], "要出门")
        self.assertEqual(d["emergency_log"][0]["time"],
                         datetime.fromtimestamp(self.c.t).strftime("%H:%M"))
        self.assertEqual(st.read_session_state(self.state_path).exit_reason, "要出门")
        self.assertEqual(s.status.week_total("emergency_exits"), 1)
        self.assertEqual(d["credited_minutes"], 10)   # 读过的照算

    def test_cancel_clears_reason(self):
        s = LockSession(clock=self.c, state_path=self.state_path, status_path=self.status_path,
                        emergency_required=4)
        s.start(minutes=30)
        s.begin_emergency()
        s.confirm_emergency()
        s.confirm_emergency(reason="有人找我")
        s.cancel_emergency()
        self.assertIsNone(s.emergency.reason)
        self.assertEqual(s.emergency.count, 0)

    def test_old_status_file_without_log_still_loads(self):
        store = StatusStore(self.status_path, clock=self.c)
        store.bump("sessions")
        # 老版本写的文件没有 emergency_log，多一个不认识的键也不该崩
        import json
        data = json.loads(self.status_path.read_text("utf-8"))
        for day in data["days"].values():
            day.pop("emergency_log", None)
            day["something_new"] = 1
        self.status_path.write_text(json.dumps(data), "utf-8")
        self.assertEqual(store.today().sessions, 1)
        self.assertEqual(store.today().emergency_log, [])


class TestLastAway(unittest.TestCase):
    def test_last_away_seconds(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        c = Clock()
        s = LockSession(clock=c, state_path=Path(tmp.name) / "s.json",
                        status_path=Path(tmp.name) / "d.json")
        s.start(minutes=30)
        c.advance(10)
        s.on_left()
        c.advance(192)
        s.on_returned()
        self.assertAlmostEqual(s.last_away, 192, places=3)


if __name__ == "__main__":
    unittest.main()
