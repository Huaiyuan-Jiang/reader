"""紧急出口（界面稿板 8）：4 步，第 2 步选原因。

跟「离开」不一样：离开（板 7）是暂停，回来接着读；紧急出口是这次到此结束、退出全屏。

防连点冲出去的几道：
- 「走」的按钮每一步**换位置、换说法**：左下 → 右下 → 上面（两个按钮里上面那个）→ 中间。
  「留下」也换说法，而且常常就站在上一步「走」的位置上 —— 连点两下的第二下落在「留下」上。
- 每一步出来后 **1 秒内**按钮不响应（ARM_DELAY）；第 3 步要等 **5 秒**（STEP3_WAIT），按钮上倒数。
- **Esc = 留下**（任何一步）；**Enter 永远不会触发「走」**；「走」的按钮不拿键盘焦点，只能用鼠标点。
- 每一步都写「第 N 步，共 4 步」，也都说清会记下来；措辞不责备。

本模块只画界面和管自己的步骤；记录由调用方接 ``confirmed`` 信号去调 LockSession.confirm_emergency。
时间可注入（clock），倒数和「1 秒不响应」都按 clock 算，QTimer 只负责刷新显示 —— 单测不用真等。

ExitFlow 是盖在整个读书界面上的一层（半透明遮罩 + 中间一张卡），不是另开的窗口：
另开窗口会跟置顶的读书窗口抢层级，也会让系统以为焦点离开了。
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QButtonGroup, QGridLayout, QHBoxLayout, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from . import theme
from .cards import _Floating, button, label, restyle, rgba, rich

STEPS = 4
ARM_DELAY = 1.0        # 每一步出来后这么久内按钮不响应
STEP3_WAIT = 5.0       # 第 3 步要等这么久才能点「确定要走」
REASONS = ["有人找我", "要出门", "身体不舒服", "电脑上有别的急事要马上处理", "别的原因"]

# 「走」按钮在卡上的位置（测试用这个名字核对）
LEAVE_POS = {1: "bottom-left", 2: "bottom-right", 3: "top", 4: "center"}


def _danger_qss() -> str:
    return (f"QPushButton {{ background: {rgba('red', 0.13)}; border: 1px solid {rgba('red', 0.55)};"
            f" color: {theme.tok('redt')}; font-weight: 600; border-radius: 8px; padding: 0 22px; }}"
            f" QPushButton:hover {{ background: {rgba('red', 0.2)}; }}")


def _radio_qss() -> str:
    t = theme.THEMES[theme.current()]
    return (
        f"QRadioButton {{ color: {t['t2']}; font-size: 14px; padding: 7px 10px; border-radius: 8px;"
        f" spacing: 10px; background: transparent; }}"
        f" QRadioButton:hover {{ background: {t['bg4']}; }}"
        f" QRadioButton:checked {{ background: {t['bg4']}; color: {t['t1']}; }}"
        f" QRadioButton::indicator {{ width: 13px; height: 13px; border-radius: 8px;"
        f" border: 1.5px solid {t['l3']}; background: transparent; }}"
        f" QRadioButton::indicator:checked {{ border-color: {t['t1']};"
        f" background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,"
        f" stop:0 {t['t1']}, stop:0.5 {t['t1']}, stop:0.62 transparent); }}"
    )


class ExitFlow(QWidget):
    """盖在读书界面上的紧急出口。open() 从第 1 步开始。"""

    stayed = Signal()                # 留下（任何一步的「留下」或 Esc）
    confirmed = Signal(int, object)  # 第 n 步点了「走」（第 2 步带原因）；n == 4 就是真走了

    CARD_W, CARD_H = 560, 430

    def __init__(self, parent: QWidget, *, clock: Callable[[], float] = time.monotonic,
                 facts: Callable[[], dict] | None = None):
        super().__init__(parent)
        self._clock = clock
        self._facts = facts or (lambda: {})
        self.step = 0
        self.reason: str | None = None
        self.shown_at = 0.0
        self._prev_focus: QWidget | None = None
        self.leave_btn: QPushButton | None = None
        self.stay_btn: QPushButton | None = None
        self._wait_note = None
        self._radios: QButtonGroup | None = None
        self.card = _Floating(self)
        self.card.setFixedSize(self.CARD_W, self.CARD_H)
        self.card.show()
        self._body: QWidget | None = None
        self.setFocusPolicy(Qt.StrongFocus)
        self._ticker = QTimer(self)
        self._ticker.setInterval(200)
        self._ticker.timeout.connect(self.refresh)
        self.hide()

    # ── 对外 ─────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self.step > 0

    def open(self) -> None:
        """从第 1 步开始。焦点给自己（是主人自己点的「紧急出口」，所以可以挪焦点），好接 Esc。"""
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        self._prev_focus = fw if fw is not None and not self.isAncestorOf(fw) else None
        self.reason = None
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.goto(1)
        self.setFocus(Qt.OtherFocusReason)
        self._ticker.start()

    def close_flow(self) -> None:
        """收起（不发信号）。时间到了、或者已经走了，调用方用这个。"""
        self._ticker.stop()
        self.step = 0
        self.hide()
        if self._prev_focus is not None:
            try:
                self._prev_focus.setFocus(Qt.OtherFocusReason)
            except RuntimeError:
                pass
        self._prev_focus = None

    def goto(self, step: int) -> None:
        self.step = step
        self.shown_at = self._clock()
        self._build(step)
        self.refresh()

    def armed(self, now: float | None = None) -> bool:
        """这一步的按钮现在响不响应。"""
        now = self._clock() if now is None else now
        wait = STEP3_WAIT if self.step == 3 else ARM_DELAY
        return now - self.shown_at >= wait

    def wait_left(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return max(0.0, STEP3_WAIT - (now - self.shown_at)) if self.step == 3 else 0.0

    def can_leave(self, now: float | None = None) -> bool:
        if not self.armed(now):
            return False
        if self.step == 2 and not self.reason:
            return False
        return True

    def leave_position(self) -> str:
        return LEAVE_POS.get(self.step, "")

    # ── 点按钮 ─────────────────────────────────────────────
    def try_leave(self) -> bool:
        if not self.active or not self.can_leave():
            return False
        n = self.step
        reason = self.reason if n == 2 else None
        if n < STEPS:
            self.goto(n + 1)
        else:
            self.close_flow()
        self.confirmed.emit(n, reason)
        return True

    def try_stay(self, *, force: bool = False) -> bool:
        """留下。按钮要等 1 秒才响应；Esc（force）随时都行。"""
        if not self.active:
            return False
        if not force and not self.armed_for_stay():
            return False
        self.close_flow()
        self.stayed.emit()
        return True

    def armed_for_stay(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return now - self.shown_at >= ARM_DELAY

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Escape:
            self.try_stay(force=True)
            e.accept()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            e.accept()          # Enter / 空格不做任何事，更不会「走」
            return
        e.accept()              # 别的键也不往下传（书页后面别翻页）

    def mousePressEvent(self, e):  # noqa: N802 —— 点遮罩空白处不做任何事
        e.accept()

    # ── 显示 ─────────────────────────────────────────────
    def refresh(self) -> None:
        if not self.active or self.leave_btn is None:
            return
        now = self._clock()
        if self.step == 3:
            left = self.wait_left(now)
            ok = left <= 0
            self.leave_btn.setEnabled(ok)
            self.leave_btn.setText("确定要走" if ok else f"确定要走（{math.ceil(left)}）")
            if self._wait_note is not None:
                self._wait_note.setText("真的等不了，现在可以走了。走了会记一次紧急退出。"
                                        if ok else "真的等不了，5 秒后可以继续。走了会记一次紧急退出。")
        elif self.step == 2:
            self.leave_btn.setEnabled(bool(self.reason))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._place_card()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._place_card()

    def _place_card(self) -> None:
        self.card.move((self.width() - self.CARD_W) // 2, max(20, (self.height() - self.CARD_H) // 2 - 20))

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), theme.color("scrim"))

    def _head(self, lay: QVBoxLayout, n: int, title: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(label(title, 19, "t1", 600, wrap=False))
        row.addStretch(1)
        row.addWidget(label(f"第 {n} 步，共 {STEPS} 步", 12, "t4", wrap=False, tnum=True), 0, Qt.AlignBottom)
        lay.addLayout(row)

    def _build(self, step: int) -> None:
        if self._body is not None:
            self._body.hide()
            self._body.deleteLater()
        self._wait_note = None
        self._radios = None
        body = QWidget(self.card)
        body.setGeometry(QRect(0, 0, self.CARD_W, self.CARD_H))
        lay = QVBoxLayout(body)
        lay.setContentsMargins(26, 24, 26, 24)
        lay.setSpacing(0)
        f = self._facts()
        read = int(f.get("read", 0))
        left = int(f.get("left", 0))
        if step == 1:
            self._head(lay, 1, "要提前结束这次吗？")
            lay.addSpacing(14)
            lay.addWidget(label(rich(f"这次已经读了 **{read}** 分钟，还剩 **{left}** 分钟。"), 14, "t2", lh=1.75))
            lay.addSpacing(8)
            lay.addWidget(label(f"现在走，读过的 {read} 分钟照样算进今天，剩下的晚点可以再补。", 14, "t2", lh=1.75))
            lay.addSpacing(8)
            lay.addWidget(label("走的话会记一次「紧急退出」。", 14, "t3", lh=1.75))
            lay.addSpacing(8)
            lay.addWidget(label("只是临时看一眼别的？不用走这里：切出去计时会停，回来点一下就接着读。", 13, "t3",
                                lh=1.75))
            lay.addStretch(1)
            row = QHBoxLayout()
            self.leave_btn = button("有急事，得走")
            self.stay_btn = button("回去接着读", "pri", focus=True)
            row.addWidget(self.leave_btn)
            row.addStretch(1)
            row.addWidget(self.stay_btn)
            lay.addLayout(row)
        elif step == 2:
            self._head(lay, 2, "是什么急事？")
            lay.addSpacing(8)
            lay.addWidget(label("选一个就行，会跟这次的紧急退出记录放在一起。", 14, "t3", lh=1.75))
            lay.addSpacing(14)
            grp = QButtonGroup(body)
            grp.setExclusive(True)
            for i, r in enumerate(REASONS):
                rb = QRadioButton(r)
                rb.setCursor(Qt.PointingHandCursor)
                restyle(rb, _radio_qss)
                grp.addButton(rb, i)
                lay.addWidget(rb)
                lay.addSpacing(4)
            grp.idClicked.connect(self._pick_reason)
            self._radios = grp
            lay.addStretch(1)
            row = QHBoxLayout()
            self.stay_btn = button("不走了", "pri", focus=True)
            self.leave_btn = button("选好了，下一步")
            row.addWidget(self.stay_btn)
            row.addStretch(1)
            row.addWidget(self.leave_btn)
            lay.addLayout(row)
        elif step == 3:
            self._head(lay, 3, "急事要紧，书明天也在")
            lay.addSpacing(12)
            lay.addWidget(label(f"再想一下：还剩 {left} 分钟。要是能等，读完再去也来得及。", 14, "t2", lh=1.75))
            lay.addSpacing(8)
            self._wait_note = label("", 14, "t3")
            lay.addWidget(self._wait_note)
            lay.addStretch(1)
            self.leave_btn = button("确定要走", height=40)
            self.stay_btn = button("还是读完吧", "pri", focus=True, height=40)
            lay.addWidget(self.leave_btn)          # 上面那个是「走」，要先等 5 秒
            lay.addSpacing(10)
            lay.addWidget(self.stay_btn)
        else:
            self._head(lay, 4, "最后确认")
            lay.addSpacing(14)
            today, target = int(f.get("today", 0)), int(f.get("target", 45))
            gap = max(0, target - today)
            when: datetime = f.get("now") or datetime.now()
            stamp = f"{when.month} 月 {when.day} 日 {when:%H:%M}"
            rows = [("这次", f"读了 {read} 分钟，算进今天"),
                    ("今天", f"{today} / {target} 分钟" + (f"，还差 {gap}" if gap else "，已经读满")),
                    ("记一次", f"紧急退出 · {stamp}" + (f" · {self.reason}" if self.reason else "")),
                    ("这周", f"第 {int(f.get('week_exits', 0)) + 1} 次")]
            grid = QGridLayout()
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(7)
            for i, (k, v) in enumerate(rows):
                grid.addWidget(label(k, 14, "t4", wrap=False), i, 0)
                grid.addWidget(label(v, 14, "t2", tnum=True), i, 1)
            grid.setColumnStretch(1, 1)
            lay.addLayout(grid)
            lay.addSpacing(22)
            self.leave_btn = button("结束这次，退出全屏", height=40)
            restyle(self.leave_btn, _danger_qss)
            lay.addWidget(self.leave_btn, 0, Qt.AlignHCenter)
            lay.addStretch(1)
            row = QHBoxLayout()
            row.addStretch(1)
            self.stay_btn = button("算了，回去读", focus=True)
            row.addWidget(self.stay_btn)
            lay.addLayout(row)
        # 「走」只认鼠标：不拿焦点、不做默认按钮
        self.leave_btn.setFocusPolicy(Qt.NoFocus)
        self.leave_btn.setAutoDefault(False)
        self.leave_btn.clicked.connect(self.try_leave)
        self.stay_btn.clicked.connect(lambda: self.try_stay())
        body.show()
        self._body = body

    def _pick_reason(self, idx: int) -> None:
        self.reason = REASONS[idx]
        self.refresh()

    def pick_reason(self, reason: str) -> None:
        """测试 / 程序里选原因（等于点了那一项）。"""
        if self._radios is None or reason not in REASONS:
            return
        i = REASONS.index(reason)
        self._radios.button(i).setChecked(True)
        self._pick_reason(i)
