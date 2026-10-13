"""读书界面（界面稿板 3、5、6、7、8、9、10①）：把顶条、左栏 AI、右边书页、计时、锁屏串起来。

    ┌ 顶条 strip.py ─────────────────────────────────────────────┐
    │《书名》· 小节        这次还剩 19 分钟       今天 26 / 45 │ 紧急出口 │
    ├──────────────┬─────────────────────────────────────────────┤
    │ AiPanel（C） │ BookView（B）+ 盖在上面的卡（cards.py）       │
    └──────────────┴─────────────────────────────────────────────┘
    紧急出口（exit_flow.py）盖在整个界面上；副屏遮罩的字见 overlay_text.py。

## 锁屏（2026-09-24 改过）
只用普通窗口功能：ReadingWindow 无边框、全屏、置顶。**不装键盘钩子、不抢焦点、不看门重启。**
焦点离开 → LeaveWatcher 报 left → 暂停、记一次离开、书页上一张卡；主人点一下窗口，系统把焦点给回来
→ returned → 接着计时。我们自己从不把焦点抢回来。

## 计时（LockSession，allow_overtime=True）
- 书页 activity、AI 栏 activity、在窗口里点鼠标 / 滚轮 → ping()。
- 发呆（5 分钟）：卡 + 蒙层；回来（点「在读」、翻页、滚动、点一下）就把那 5 分钟补回来。
- 离开优先于发呆：两个同时发生只显示离开那张卡。
- 时间到：不踢人（板 10①）——撤掉置顶和副屏遮罩，书页上方一条「27 分钟读够了」，
  「再读一会儿」（多读的也算进今天）/「结束，看读书笔记」。结束时后台写读书笔记，存进 Library，再发 finished。

## 小测
进入一节就在后台 make_quiz 提前出题（约 13 秒），set_barrier(这一节)。主人想翻过这一节末尾 → barrier_hit
→ AiPanel.show_quiz。答错 → jump_to → BookView.mark_source。答完 → Library.mark_quiz_done、barrier 挪到下一节。
已经答过的节不再出；扫描版 / 没登录 / 出题失败 / 时间到了之后：不挡。
2026-09-25 起：**不挡**（BookView.BLOCK = False）。一页页读过节末就出题，但照样能往后翻；
目录、跳转随便跳，跳过去的节不出题；目录、版权页这种节不出题；题可以「跳过这题」（不算对错，这一节不再出）；
有题的时候左栏照样能问。

## 保存
位置随时存（翻页后 1 秒存一次），高亮一变就存，都进 Library（A 写的）。

## 开发开关 READER_NO_LOCK=1
设了这个环境变量，读书界面用普通窗口（1280×800，有标题栏），**不调 show_locked()、不铺副屏遮罩**；
计时、离开、发呆、小测、紧急出口、笔记都照常是真的。开发和端到端测试一律用它，别在主人的电脑上真锁屏。
开发窗口点 × 关掉 = 顶条的「结束」。

## 依赖怎么换
所有外部零件都经 ReadingDeps 拿：书页、AI 栏、Companion、登录检查、后台线程、窗口、遮罩、LeaveWatcher。
测试和演示换成假的，真程序用默认的。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

from ..books.model import is_front_matter_title
from . import theme
from .cards import IdleCard, LeaveCard, NoticeLine, TimeUpBar, away_notice, label
from .exit_flow import STEPS, ExitFlow
from .overlay_text import AWAY, IDLE, READING, OverlayText
from .strip import Strip, minutes_left

QUIZ_WAIT = 20.0       # 翻到节末题还没出好：最多挡这么久，出不来就放行（不因为 AI 慢把人卡住）
SAVE_DELAY_MS = 1000   # 翻页后多久存一次位置
TARGET_MINUTES = 45
STACK_GAP = 8          # 书页顶上叠着的几条（出处小条、「刚才离开了……」、读够了）之间的空


def no_lock() -> bool:
    """开发开关：环境变量 READER_NO_LOCK=1 → 不锁屏（普通窗口、没有副屏遮罩），别的都是真的。"""
    return os.environ.get("READER_NO_LOCK", "").strip().lower() in ("1", "true", "yes", "on")


def page_number(label_text) -> int | None:
    """「第 134 页」→ 134；认不出来返回 None。"""
    m = re.search(r"\d+", str(label_text or ""))
    return int(m.group()) if m else None


# ── 后台跑 ───────────────────────────────────────────────────
class _Relay(QObject):
    done = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any], relay: _Relay):
        super().__init__()
        self.fn, self.relay = fn, relay

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # noqa: BLE001 —— 交回界面线程去处理
            result = e
        self.relay.done.emit(result)


_live: set[_Relay] = set()


def run_in_background(fn: Callable[[], Any], callback: Callable[[Any], None]) -> None:
    """fn 在线程池里跑，结果（或异常对象）回到界面线程交给 callback。"""
    relay = _Relay()
    _live.add(relay)

    def _back(result):
        _live.discard(relay)
        callback(result)
    relay.done.connect(_back, Qt.QueuedConnection)
    QThreadPool.globalInstance().start(_Job(fn, relay))


# ── 默认依赖（真零件） ───────────────────────────────────────
def _default_view(book):
    if getattr(book, "kind", "") == "pdf":
        from .bookview.pdf_view import PdfView
        return PdfView()
    from .bookview.epub_view import EpubView
    return EpubView()


def _default_panel():
    from .ai_panel import AiPanel
    return AiPanel()


def _default_companion(book):
    from ..ai.companion import Companion
    return Companion(book)


def _default_logged_in() -> bool:
    from ..ai import provider
    return provider.logged_in()


def _default_open_book(path: str):
    from ..books import open_book
    return open_book(path)


def _default_window():
    from ..lock.windows import ReadingWindow
    return ReadingWindow()


def _default_overlays():
    from ..lock.windows import OverlayManager
    return OverlayManager()


def _default_watcher():
    from ..lock.windows import LeaveWatcher
    return LeaveWatcher()


def release_on_top(window: QWidget) -> None:
    """时间到了：窗口不再压在最上面（还是全屏，主人可能正读到半句）。只改窗口标志，不碰别的程序。"""
    release = getattr(window, 'release_shortcuts', None)
    if release is not None:
        release()
    visible = window.isVisible()
    full = window.isFullScreen()
    window.setWindowFlag(Qt.WindowStaysOnTopHint, False)
    if visible and not window.isVisible():       # 改标志会把窗口藏起来，重新显示
        window.showFullScreen() if full else window.show()


@dataclass
class ReadingDeps:
    open_book: Callable[[str], Any] = _default_open_book
    make_view: Callable[[Any], Any] = _default_view
    make_panel: Callable[[], Any] = _default_panel
    make_companion: Callable[[Any], Any] = _default_companion
    logged_in: Callable[[], bool] = _default_logged_in
    run_bg: Callable[[Callable[[], Any], Callable[[Any], None]], None] = run_in_background
    make_window: Callable[[], Any] = _default_window
    make_overlays: Callable[[], Any] = _default_overlays
    make_watcher: Callable[[], Any] = _default_watcher
    release_top: Callable[[Any], None] = release_on_top
    clock: Callable[[], float] = time.time
    state_path: Path | None = None      # None = LockSession 的默认路径（%LOCALAPPDATA%\reader\）
    status_path: Path | None = None
    idle_timeout: float = 300.0
    extra: dict = field(default_factory=dict)


# ── 书页那块：书 + 盖在上面的卡 ─────────────────────────────
class BookArea(QWidget):
    """放 BookView，上面浮着发呆卡 / 离开卡 / 一行提示 / 读够了的条。位置在 resizeEvent 里摆。

    书页顶上可能同时有好几条：书页自己的出处小条（「跳到了原文 · 第 N 页」，B 的 BackPill）、
    「刚才离开了……」、「读够了」。从上往下叠着放，谁也不盖谁。出处小条显示 / 收起时重新摆。
    书还在打开时（大 PDF 要一两秒）中间写一行「正在打开……」。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view: QWidget | None = None
        self._pill: QWidget | None = None
        self.loading = label("", 14, "t4", wrap=False, parent=self)
        self.loading.hide()
        self.idle_card = IdleCard(self)
        self.leave_card = LeaveCard(self)
        self.notice = NoticeLine(self)
        self.time_up = TimeUpBar(self)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.StrongFocus)       # 书还没打开时焦点先放这里（不能落到左栏输入框上）
        theme.style(self, "BookArea {{ background: {paper}; }}")

    def set_loading(self, text: str | None) -> None:
        self.loading.setText(text or "")
        self.loading.setVisible(bool(text))
        self._place()

    def set_view(self, view: QWidget) -> None:
        self.view = view
        view.setParent(self)
        view.show()
        view.lower()
        self._pill = getattr(view, "pill", None)
        if self._pill is not None:
            self._pill.installEventFilter(self)
        self._place()

    def eventFilter(self, obj, ev):  # noqa: N802 —— 出处小条出来 / 收起 / 变宽：下面几条跟着挪
        if obj is self._pill and ev.type() in (QEvent.Show, QEvent.Hide, QEvent.Resize, QEvent.Move):
            self._place()
        return False

    def stack_top(self) -> int:
        """书页顶上第一条能放的位置：出处小条在的话放在它下面。"""
        pill = self._pill
        if pill is not None and not pill.isHidden():
            try:
                bottom = pill.mapTo(self, pill.rect().bottomLeft()).y()
            except RuntimeError:
                return 14
            return max(14, bottom + 1 + STACK_GAP)
        return 14

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._place()

    def _place(self) -> None:
        w, h = self.width(), self.height()
        if self.view is not None:
            self.view.setGeometry(0, 0, w, h)
        for card, top in ((self.idle_card, 190), (self.leave_card, 170)):
            card.adjustSize()
            hfw = card.heightForWidth(card.width())
            if hfw > 0:
                card.resize(card.width(), hfw)
            card.move((w - card.width()) // 2, min(top, max(20, h - card.height() - 60)))
        self.loading.adjustSize()
        self.loading.move((w - self.loading.width()) // 2, max(0, h // 2 - 40))
        # 顶上几条从上往下叠：出处小条（书页里）→「刚才离开了……」→ 读够了
        y = self.stack_top()
        self.notice.adjustSize()
        self.notice.move((w - self.notice.width()) // 2, y)
        if not self.notice.isHidden():
            y += self.notice.height() + STACK_GAP
        self.time_up.setFixedWidth(max(300, w - 48))
        self.time_up.adjustSize()
        self.time_up.move(24, y)
        for c in (self.loading, self.notice, self.time_up, self.idle_card, self.leave_card):
            c.raise_()

    def show_card(self, card: QWidget) -> None:
        card.show()
        self._place()
        card.raise_()


class ReadingWidget(QWidget):
    """整屏内容：顶条 + 左栏 + 书页区；紧急出口盖在最上面。"""

    def __init__(self, panel: QWidget, *, clock: Callable[[], float], facts: Callable[[], dict],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("ReadingWidget")
        theme.style(self, "QWidget#ReadingWidget {{ background: {bg2}; }}")
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.strip = Strip(self, TARGET_MINUTES)
        col.addWidget(self.strip)
        mid = QHBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        self.panel = panel
        mid.addWidget(panel)
        self.area = BookArea(self)
        mid.addWidget(self.area, 1)
        col.addLayout(mid, 1)
        self.exit_flow = ExitFlow(self, clock=clock, facts=facts)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.exit_flow.isVisible():
            self.exit_flow.setGeometry(self.rect())


# ── 总控 ─────────────────────────────────────────────────────
# 读不到这么多分钟、也没提问没答题，结束时不自动写读书笔记（2026-09-24：整合测试发现 0 分钟也写）
NOTE_MIN_MINUTES = 3


class ReadingScreen(QObject):
    """一次读书。A 的 app.py：ReadingScreen(entry, minutes, library)，连 finished，调 start()。"""

    finished = Signal(dict)   # 这次的总结（见 summary()）；reason: "done" / "emergency_exit" / "open_failed"
    # 读完（reason "done"）马上发 finished，不等笔记：笔记由读书笔记页调总结里的 rewrite_note() 在后台写
    # （写的时候纸上显示「Claude 在写读书笔记…」，板 10②）。紧急出口不写笔记。

    def __init__(self, entry, minutes: int, library, *, deps: ReadingDeps | None = None,
                 locked: bool | None = None, lock_session=None):
        super().__init__()
        self.entry = entry
        self.minutes = int(minutes)
        self.library = library
        self.deps = deps or ReadingDeps()
        self.locked = (not no_lock()) if locked is None else locked
        self._clock = self.deps.clock

        from ..lock.session import LockSession
        kw = {}
        if self.deps.state_path is not None:
            kw["state_path"] = self.deps.state_path
        if self.deps.status_path is not None:
            kw["status_path"] = self.deps.status_path
        self.session = lock_session or LockSession(
            clock=self._clock, idle_timeout=self.deps.idle_timeout, emergency_required=STEPS,
            target_minutes=TARGET_MINUTES, allow_overtime=True, **kw)

        self.book = None
        self.view = None
        self.companion = None
        self.logged_in: bool | None = None       # None = 还在查
        self.window = None
        self.overlays = None
        self.overlay_text: OverlayText | None = None
        self.watcher = None
        self.today_base = 0.0
        self.start_loc = None
        self.started_at: float | None = None
        self.ended = False
        self._disposed = False
        self._summary: dict | None = None

        # 小测
        self.quiz_done: set[int] = set()
        self.quizzes: dict[int, Any] = {}        # 小节号 → Quiz / 失败原因（str）/ None（在出）
        self.failed: set[int] = set()
        self.skipped: set[int] = set()           # 主人点了「跳过这题」的节（这次不再出）
        self.gate: int | None = None             # 现在挡在哪一节末尾
        self.showing = None                      # 左栏正在出的那道题
        self.want: int | None = None             # 翻到节末了但题还没出好
        self.want_since = 0.0
        self.results: list[dict] = []
        self.shown_quizzes: list = []
        self.quiz_off_reason = ""                # 不出题的原因（调试用）
        self.last_quiz_section: int | None = None   # 最近答完的那一节（「接着读下一节 ›」从它往后翻）
        self._jumping = False                    # 正在跳到小测出处（这时书页报的位置先不往左栏送）

        self.panel = self.deps.make_panel()
        self.widget = ReadingWidget(self.panel, clock=self._clock, facts=self._exit_facts)
        self.strip = self.widget.strip
        self.area = self.widget.area
        self.exit_flow = self.widget.exit_flow
        self.strip.set_book(entry.title, "正在打开…")

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self.tick)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self.save_position)

        s = self.session
        s.idle.connect(self._on_idle)
        s.left.connect(self._on_left)
        s.returned.connect(self._on_returned)
        s.time_up.connect(self._on_time_up)
        s.finished.connect(self._on_session_finished)

        a = self.area
        a.idle_card.resume.connect(lambda: self.session.ping())
        a.leave_card.come_back.connect(lambda: self.session.on_returned())
        a.time_up.more.connect(self._read_more)
        a.time_up.finish.connect(self.finish)
        self.strip.exit_clicked.connect(self._open_exit)
        self.strip.finish_clicked.connect(self.finish)
        self.exit_flow.confirmed.connect(self._exit_confirmed)
        self.exit_flow.stayed.connect(lambda: self.session.cancel_emergency())

        p = self.panel
        p.activity.connect(self._activity)
        p.jump_to.connect(self._on_jump_to)
        p.quiz_finished.connect(self._on_quiz_finished)
        if hasattr(p, "next_section"):           # 小测卡「接着读下一节 ›」（C 多给的信号，可有可无）
            p.next_section.connect(self._on_next_section)
        if hasattr(p, "quiz_skipped"):           # 小测卡「跳过这题」
            p.quiz_skipped.connect(self._on_quiz_skipped)

    # ── 生命周期 ─────────────────────────────────────────
    def start(self) -> None:
        """打开书、进锁屏（置顶窗口 + 副屏遮罩）、开始计时。

        书在后台打开（大 PDF 要一秒左右），这段时间书页区写「正在打开……」。
        键盘焦点一开始就放在书页区（打开后给书页），←→ 马上能翻页，绝不落在左栏输入框上。
        READER_NO_LOCK=1（或 locked=False）：普通窗口，不锁屏、不铺副屏遮罩。
        """
        self.area.set_loading(f"正在打开《{self.entry.title}》…")
        self.area.setFocus(Qt.OtherFocusReason)
        if self.locked:
            self.window = self.deps.make_window()
            self.window.set_content_widget(self.widget)
            self.window.show_locked()
            self.overlays = self.deps.make_overlays()
            self.overlays.show()
            self.overlay_text = OverlayText(self.overlays)
        else:
            self.widget.setWindowTitle(f"reader · 《{self.entry.title}》（开发模式：没有锁屏）")
            self.widget.resize(1280, 800)
            self.widget.show()
            self.widget.activateWindow()
        self.area.setFocus(Qt.OtherFocusReason)
        self.watcher = self.deps.make_watcher()
        if self.watcher is not None:
            self.watcher.left.connect(self._watch_left)
            self.watcher.returned.connect(self._watch_returned)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.deps.run_bg(lambda: self.deps.open_book(self.entry.path), self._on_book)
        self.deps.run_bg(self.deps.logged_in, self._on_auth)

    def _on_book(self, book) -> None:
        if self.ended:
            return
        self.area.set_loading(None)
        if isinstance(book, BaseException) or book is None:
            self._close_and_emit({**self._base_summary(), "reason": "open_failed",
                                  "error": "这本书无法打开，请检查文件是否完整或需要密码，再重新选择文件。"})
            return
        self.book = book
        self.view = self.deps.make_view(book)
        v = self.view
        v.activity.connect(self._activity)
        v.location_changed.connect(self._on_location)
        v.ask_ai.connect(self._on_ask_ai)
        v.highlights_changed.connect(self._save_highlights)
        v.barrier_hit.connect(self._on_barrier_hit)
        self.area.set_view(v)
        try:
            refresh = getattr(self.library, "refresh_structure", None)
            if refresh is not None:
                refresh(self.entry.id, book)     # 在 quiz_done 之前：小节变了会清掉旧的答题记号
        except Exception:  # noqa: BLE001
            pass
        try:
            hls = list(self.library.highlights(self.entry.id))
        except Exception:  # noqa: BLE001
            hls = []
        try:
            self.quiz_done = set(self.library.quiz_done(self.entry.id))
        except Exception:  # noqa: BLE001
            self.quiz_done = set()
        v.open(book, getattr(self.entry, "position", None), hls)
        self.focus_book()
        self.start_loc = v.current_loc()
        # 计时从书真的打开了才开始
        self.session.start(self.minutes)
        self.started_at = self._clock()
        self.today_base = float(self.session.daily().get("credited_seconds", 0))
        self._tick.start()
        self._on_location(self.start_loc)
        if self.logged_in is not None:
            self._setup_ai()
        self.refresh()

    def _on_auth(self, ok) -> None:
        self.logged_in = bool(ok) and not isinstance(ok, BaseException)
        if self.book is not None and not self.ended:
            self._setup_ai()

    def _setup_ai(self) -> None:
        if self.logged_in:
            try:
                self.companion = self.deps.make_companion(self.book)
            except Exception:  # noqa: BLE001
                self.companion = None
        self.panel.set_companion(self.companion)
        if self.view is not None:
            self._regate(self._section_of(self.view.current_loc()))

    def focus_book(self) -> None:
        """键盘焦点给书页（←→ 翻页）。开书时、「接着读下一节」后调。"""
        target = self.view if self.view is not None else self.area
        target.setFocus(Qt.OtherFocusReason)

    # ── 每秒 ─────────────────────────────────────────────
    def tick(self) -> None:
        if not self.session.active:
            return
        self.session.tick()
        if self.want is not None and self._clock() - self.want_since >= QUIZ_WAIT:
            # 翻到节末了，题等了这么久还没出好：不挡了
            idx = self.want
            self.want = None
            self._quiz_failed(idx, "出题太慢，这一节先不考")
        self.refresh()

    def refresh(self) -> None:
        """把计时状态画到顶条、卡片、副屏。"""
        s = self.session
        if s.session is None:
            return
        t = s.timer
        planned = self.minutes * 60
        credited = t.credited
        remaining = t.remaining(self.minutes)
        today = self._today_credited()
        self.strip.set_times(remaining, credited, planned, today, s.extra_seconds)
        self.strip.set_state(self.mode())
        if self.area.leave_card.isVisible():
            self.area.leave_card.set_info(minutes_left(remaining), self._leaves_today)
        if self.overlay_text is not None:
            st = AWAY if t.left_now else IDLE if t.idle else READING
            self.overlay_text.update(st, remaining)

    def _today_credited(self) -> float:
        """Current day's total, including this session's unflushed credit."""
        today = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d")
        persisted = float(self.session.status.today().credited_seconds)
        session_credit = self.session.timer.credited_by_day.get(today, 0.0)
        already_saved = self.session._reported_by_day.get(today, 0.0)
        return persisted + session_credit - already_saved

    def mode(self) -> str:
        s = self.session
        if s.reached:
            return "done" if self.area.time_up.isVisible() else "extra"
        if s.timer.left_now:
            return "away"
        if s.timer.idle:
            return "pause"
        return "run"

    # ── 活动 / 发呆 / 离开 ───────────────────────────────
    def _activity(self, *_a) -> None:
        self.session.ping()
        self.refresh()

    def eventFilter(self, obj, ev):  # noqa: N802 —— 窗口里点一下、滚一下都算在读
        if obj is self.widget and ev.type() == QEvent.Close and not self.locked and not self.ended:
            # 开发模式的普通窗口点了 ×：当成顶条的「结束」（书还没打开就直接收摊）
            if self.session.active:
                self.finish()
            else:
                self._close_and_emit({**self._base_summary(), "reason": "open_failed",
                                      "error": "书还没打开窗口就关了"})
            return False
        if ev.type() in (QEvent.MouseButtonPress, QEvent.Wheel) and isinstance(obj, QWidget) \
                and self.session.active and obj.window() is self.widget.window():
            if self.exit_flow.active or obj is self.strip.exit_btn:
                return False
            was_idle = self.session.timer.idle
            self.session.ping()
            if was_idle:
                self.refresh()
        return False

    def _on_idle(self, on: bool) -> None:
        a = self.area
        if on:
            # 离开优先；读够以后不再打扰，只在顶条写暂停
            if not self.session.timer.left_now and not self.session.reached:
                a.show_card(a.idle_card)
                self._dim(True)
        else:
            a.idle_card.hide()
            if not a.leave_card.isVisible():
                self._dim(False)
        self.refresh()

    def _watch_left(self) -> None:
        self.session.on_left()

    def _watch_returned(self) -> None:
        self.session.on_returned()

    def _on_left(self) -> None:
        a = self.area
        a.idle_card.hide()
        if not self.session.reached:
            try:
                self._leaves_today = int(self.session.daily().get("leaves", 0))
            except Exception:  # noqa: BLE001
                self._leaves_today = 0
            a.notice.hide()
            a.leave_card.set_info(minutes_left(self.session.timer.remaining(self.minutes)),
                                  self._leaves_today)
            a.show_card(a.leave_card)
            self._dim(True)
        self.refresh()

    _leaves_today = 0

    def _on_returned(self) -> None:
        a = self.area
        was_card = a.leave_card.isVisible()
        a.leave_card.hide()
        a.idle_card.hide()
        self._dim(False)
        if was_card:
            a.notice.show_text(away_notice(self.session.last_away))
            self.area._place()
        self.refresh()

    def _dim(self, on: bool) -> None:
        if self.view is not None:
            self.view.set_dimmed(on)

    # ── 书页 ─────────────────────────────────────────────
    def _section_of(self, loc):
        if self.book is None or loc is None:
            return None
        sec = self.book.section_at(loc)
        return sec.idx if sec is not None else None

    def _on_location(self, loc, page_text: str | None = None) -> None:
        """书页报位置（翻页、跳转；EPUB 全书排完后页码准了也会再报一次）：顶条的小节、左栏「看得到」、存位置。

        page_text：左栏要写的页码（跳到小测出处时用出处那页，不用屏幕中间那页）。
        """
        if self.book is None or loc is None or self._jumping:
            return
        sec = self.book.section_at(loc)
        # 「接着读下一节」跳过去时，下一节常从页中间开始：屏幕最上面那段还属于上一节。
        # 停在这一页时按主人要读的那一节显示；一翻页就回到按位置算
        pin = getattr(self, "_pin", None)
        where = loc
        if pin is not None:
            if pin[1] is None:
                pin[1] = loc
            if loc == pin[1]:
                if sec is not None and sec.idx == pin[0] - 1:
                    sec = self.book.sections[pin[0]]
                    where = sec.start
            else:
                self._pin = None
        # 写「读到哪儿」用目录的章名（Book.place_title），不用小节名：小节会并、会切，跟书对不上
        place = self.book.place_title(where) if hasattr(self.book, "place_title") else (sec.title if sec else "")
        self.strip.set_book(self.entry.title, place)
        label = page_text or self.view.page_label(loc)
        self.panel.set_context(loc, f"{place} · {label}" if place else label)
        if self.session.session is not None:
            self._save_timer.start()
        # 「刚才离开了……」翻页才走
        if self.area.notice.isVisible() and getattr(self, "_last_loc", None) not in (None, loc):
            self.area.notice.dismiss()
        self._last_loc = loc
        self._regate(sec.idx if sec else None)

    def _on_ask_ai(self, text: str, loc) -> None:
        self.panel.attach_quote(text, loc)

    def save_position(self) -> None:
        if self.view is None:
            return
        try:
            self.library.save_position(self.entry.id, self.view.current_loc())
        except Exception:  # noqa: BLE001 —— 存不上下次再存，不打断读书
            pass

    def _save_highlights(self) -> None:
        if self.view is None:
            return
        try:
            self.library.save_highlights(self.entry.id, list(self.view.highlights()))
        except Exception:  # noqa: BLE001
            pass

    # ── 小测 ─────────────────────────────────────────────
    def quizzes_enabled(self) -> bool:
        if self.companion is None:
            self.quiz_off_reason = "AI 没登录"
            return False
        if getattr(self.entry, "scanned", False) or getattr(self.book, "has_text", True) is False:
            self.quiz_off_reason = "扫描版"
            return False
        if self.session.reached or self.ended:
            self.quiz_off_reason = "时间到了"
            return False
        self.quiz_off_reason = ""
        return True

    def _next_gate(self, start) -> int | None:
        if self.book is None or start is None:
            return None
        for sec in self.book.sections[start:]:
            if sec.idx not in self.quiz_done and sec.idx not in self.failed and sec.idx not in self.skipped \
                    and not is_front_matter_title(sec.title):     # 目录、版权页这种不出题
                return sec.idx
        return None

    def _blocking(self) -> bool:
        """小测挡不挡着往后读（书页的 BLOCK；2026-09-25 起不挡）。"""
        return bool(getattr(self.view, "BLOCK", True))

    def _regate(self, section) -> None:
        """按主人现在在哪一节，决定挡在哪（不挡时是「读过这一节末尾就出题」的记号）。只往前挪，往回翻不挪。
        正在出题时：挡的话不动；不挡的话主人已经读到后面的节了，就挪到主人正在读的这一节（那道题主人可以不答）。"""
        if self.view is None:
            return
        if not self.quizzes_enabled():
            self._set_gate(None)
            return
        if section is None:
            return
        if self.showing is not None and (self._blocking() or section <= getattr(self.showing, "section", section)):
            return
        g = self._next_gate(section)
        if self.gate is not None and g is not None and g < self.gate:
            return
        self._set_gate(g)

    _barrier = "unset"

    def _set_gate(self, g: int | None) -> None:
        """挡在第 g 节末尾（None = 不挡），顺手在后台把这一节的题出好。"""
        self.gate = g
        if g != self._barrier and self.view is not None:
            self._barrier = g
            self.view.set_barrier(g)
        if g is not None:
            self._prefetch(g)

    def _prefetch(self, idx: int) -> None:
        if idx in self.quizzes or self.companion is None:
            return
        self.quizzes[idx] = None
        sec = self.book.sections[idx]
        comp = self.companion
        self.deps.run_bg(lambda: comp.make_quiz(sec), lambda r, i=idx: self._on_quiz_ready(i, r))

    def _on_quiz_ready(self, idx: int, result) -> None:
        if self.ended:
            return
        from ..ai.companion import Quiz
        if not isinstance(result, Quiz):
            msg = str(result) if result else "出题失败"
            self._quiz_failed(idx, msg)
            return
        self.quizzes[idx] = result
        if self.want == idx:
            self.want = None
            self._show_quiz(result)

    def _quiz_failed(self, idx: int, why: str) -> None:
        """出不了题：这一节不挡。"""
        self.quizzes[idx] = why
        self.failed.add(idx)
        if self.want == idx:
            self.want = None
        if self.gate == idx and self.showing is None:
            self._set_gate(None)
            if self.view is not None:
                self._regate(self._section_of(self.view.current_loc()))

    def _on_barrier_hit(self, idx: int) -> None:
        if not self.quizzes_enabled():
            self._set_gate(None)
            return
        q = self.quizzes.get(idx, "missing")
        if q == "missing":
            self._prefetch(idx)
            q = self.quizzes.get(idx)
        if q is None:                            # 还在出
            if self.want != idx:
                self.want = idx
                self.want_since = self._clock()
            return
        if isinstance(q, str):                   # 出题失败：不挡
            self._set_gate(None)
            return
        if not q.finished and self.showing is not q:
            self._show_quiz(q)

    def _show_quiz(self, quiz) -> None:
        if not self.quizzes_enabled():
            return
        self.showing = quiz
        if quiz not in self.shown_quizzes:
            self.shown_quizzes.append(quiz)
        self.panel.show_quiz(quiz)

    def _on_jump_to(self, loc, quote: str) -> None:
        """小测答错：书页跳到出处、标出来；然后马上告诉左栏新位置（卡上的「原文在第 N 页」取这一次的页码）。"""
        if self.view is None or loc is None:
            return
        self._jumping = True
        try:
            self.view.mark_source(loc, quote)
        finally:
            self._jumping = False
        src = getattr(self.view, "source_label", None)
        here = src() if callable(src) else None
        self._on_location(self.view.current_loc(), here or None)

    def _on_next_section(self) -> None:
        """小测卡「接着读下一节 ›」：书页翻到刚答完那一节的下一节开头，焦点回到书页。"""
        if self.view is None or self.book is None:
            return
        cur = self.last_quiz_section
        if cur is None:
            cur = self._section_of(self.view.current_loc())
        if cur is None:
            return
        nxt = cur + 1
        if 0 <= nxt < len(self.book.sections):
            self.view.clear_source()
            self._pin = [nxt, None]          # 见 _on_location：停在这一页时显示新的那一节
            self.view.goto(self.book.sections[nxt].start)
            if self._pin is not None and self._pin[1] is None:
                self._on_location(self.view.current_loc())
        self.session.ping()
        self.focus_book()
        self.refresh()

    def _on_quiz_finished(self, idx: int, correct: bool) -> None:
        q = self.quizzes.get(idx)
        tries = len(getattr(q, "attempts", []) or []) if q is not None and not isinstance(q, str) else 0
        title = self.book.sections[idx].title if self.book and 0 <= idx < len(self.book.sections) else ""
        self.results.append({"section": idx, "title": title, "correct": bool(correct), "tries": tries})
        self.last_quiz_section = idx
        self.quiz_done.add(idx)
        try:
            self.library.mark_quiz_done(self.entry.id, idx)
        except Exception:  # noqa: BLE001
            pass
        if self.showing is not None and getattr(self.showing, "section", idx) == idx:
            self.showing = None
        if self.view is not None:
            self.view.clear_source()
        if self.gate == idx:
            # 挪到下一节（下一节的题现在就开始出）
            self._set_gate(self._next_gate(idx + 1) if self.quizzes_enabled() else None)

    def _on_quiz_skipped(self, idx: int) -> None:
        """「跳过这题」：不算对错（不进答题记录、不算答对率），这一节这次不再出；挡板（记号）挪到下一节。"""
        self.skipped.add(idx)
        if self.showing is not None and getattr(self.showing, "section", idx) == idx:
            self.showing = None
        if self.want == idx:
            self.want = None
        if self.view is not None:
            self.view.clear_source()
        if self.gate == idx:
            self._set_gate(self._next_gate(idx + 1) if self.quizzes_enabled() else None)
        if self.view is not None:
            self._regate(self._section_of(self.view.current_loc()))
        self.session.ping()
        self.focus_book()

    # ── 紧急出口 ─────────────────────────────────────────
    def _open_exit(self) -> None:
        if not self.session.active or self.session.reached:
            return
        self.session.begin_emergency()
        self.exit_flow.open()

    def _exit_facts(self) -> dict:
        s = self.session
        credited = s.timer.credited if s.session else 0.0
        try:
            week = int(s.status.week_total("emergency_exits"))
        except Exception:  # noqa: BLE001
            week = 0
        return {"read": int(credited // 60),
                "left": minutes_left(s.timer.remaining(self.minutes)) if s.session else self.minutes,
                "today": int(self._today_credited() // 60), "target": TARGET_MINUTES,
                "week_exits": week, "now": datetime.fromtimestamp(self._clock())}

    def _exit_confirmed(self, step: int, reason) -> None:
        done, _ = self.session.confirm_emergency(reason=reason)
        if step >= STEPS and not done:           # 不该发生：两边步数对不上时以界面为准
            while not done and self.session.active:
                done, _ = self.session.confirm_emergency()

    # ── 时间到 ───────────────────────────────────────────
    def _on_time_up(self) -> None:
        # 不再出题；题出到一半就说一声「答不答都行」
        if self.showing is not None and not getattr(self.showing, "finished", True):
            self.panel.quiz_timeout_note()
        self.want = None
        self._set_gate(None)
        if self.exit_flow.active:
            self.exit_flow.close_flow()
            self.session.cancel_emergency()
        a = self.area
        a.idle_card.hide()
        a.leave_card.hide()
        a.notice.hide()
        self._dim(False)
        # 不踢人：撤掉置顶和副屏遮罩
        if self.window is not None:
            self.deps.release_top(self.window)
        self._drop_overlays()
        today_min = int(self._today_credited() // 60)
        a.time_up.set_info(self.minutes, today_min, TARGET_MINUTES)
        a.show_card(a.time_up)
        self.refresh()

    def _read_more(self) -> None:
        self.area.time_up.hide()
        self.refresh()

    def finish(self) -> None:
        """「结束，看读书笔记」/ 多读时顶条的「结束」。"""
        if self.session.active:
            self.session.finish()

    # ── 结束 ─────────────────────────────────────────────
    def _on_session_finished(self, status: str) -> None:
        """会话结束：存位置和高亮，**马上**发 finished。

        读完（done）且 AI 登录了：总结里带 rewrite_note（写一篇读书笔记、存进 Library、返回路径的函数，
        在工作线程里调）和 note_pending=True，读书笔记页一打开就在后台调它，纸上先写「Claude 在写读书笔记…」。
        紧急出口不写笔记（直接回书架）。
        """
        if self.ended:
            return
        self.ended = True
        self._tick.stop()
        self._save_timer.stop()
        self.save_position()
        self._save_highlights()
        self.exit_flow.close_flow()
        reason = "emergency_exit" if status == "emergency_exit" else "done"
        summary = self.summary(reason)
        if reason == "done":
            if self.companion is not None:
                summary["rewrite_note"] = self.note_writer(summary)
                if (summary.get("minutes", 0) < NOTE_MIN_MINUTES and not summary.get("asked")
                        and not summary.get("quizzes")):
                    # 读了不到几分钟、没问没答（比如刚打开就结束）：先不花额度写笔记，想要可以点按钮写
                    summary["note_skipped"] = f"这次读了不到 {NOTE_MIN_MINUTES} 分钟，没写读书笔记"
                else:
                    summary["note_pending"] = True
            else:
                summary["note_error"] = "AI 没登录，这次没有写读书笔记"
        self._close_and_emit(summary)

    def note_writer(self, summary: dict) -> Callable[[], Path]:
        """返回一个函数：调一次就让 Claude 写一篇这次的读书笔记，存进 Library，返回笔记路径。
        写不成 / 存不上就抛 RuntimeError（读书笔记页接住，写「没写成」+「重新写一次」）。
        在工作线程里调；这次读书的数据现在就取好，之后读书界面收摊了也不影响。"""
        comp, library, book_id = self.companion, self.library, self.entry.id
        start = self.start_loc
        end = self.view.current_loc() if self.view is not None else start
        if end is not None and start is not None and end < start:
            start, end = end, start
        if end is not None and self.book is not None:
            sec = self.book.section_at(end)
            if sec is not None:
                end = sec.end if end < sec.end else end    # 把主人正在读的这一节也带上
        when = datetime.fromtimestamp(self._clock())
        quizzes = list(self.shown_quizzes)
        minutes = int(summary.get("minutes") or 0)

        def write() -> Path:
            res = comp.write_note(date=f"{when:%Y-%m-%d}", start=start, end=end,
                                  quizzes=quizzes, minutes=minutes)
            text = getattr(res, "text", "") or ""
            if not getattr(res, "ok", False) or not text.strip():
                raise RuntimeError(getattr(res, "error", None) or "AI 没有写出内容")
            try:
                return Path(library.save_note(book_id, text, when))
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"读书笔记存不上：{e}") from e
        return write

    def _base_summary(self) -> dict:
        return {"book_id": self.entry.id, "title": self.entry.title, "planned": self.minutes,
                "minutes": 0, "seconds": 0, "extra_minutes": 0, "idle_deducted": 0,
                "pages": None, "quizzes": [], "quiz_results": [], "asked": 0, "highlights": 0,
                "note_path": None, "note_error": None, "note_pending": False, "rewrite_note": None,
                "exit_reason": None}

    def page_range(self) -> list[int] | None:
        """这次从第几页读到第几页：[起, 止]（页码从 1 起，按书页上显示的页码）。"""
        if self.view is None or self.start_loc is None:
            return None
        try:
            a = page_number(self.view.page_label(self.start_loc))
            b = page_number(self.view.page_label(self.view.current_loc()))
        except Exception:  # noqa: BLE001
            return None
        if a is None or b is None:
            return None
        return [min(a, b), max(a, b)]

    def summary(self, reason: str) -> dict:
        """这次的总结（finished 发的就是它）。键：
        reason（"done" / "emergency_exit" / "open_failed"）、minutes、seconds、extra_minutes、idle_deducted、
        pages（[起, 止]）、quizzes（答过的 Quiz 列表）、quiz_results（每题一行 dict）、asked、highlights、
        note_path / note_error / note_pending / rewrite_note（见 _on_session_finished）、exit_reason、today。"""
        s, t = self.session, self.session.timer
        end_loc = self.view.current_loc() if self.view is not None else None
        try:
            hl = len(self.view.highlights()) if self.view is not None else 0
        except Exception:  # noqa: BLE001
            hl = 0
        asked = len([h for h in getattr(self.companion, "history", []) if h.get("q")]) \
            if self.companion is not None else 0
        out = self._base_summary()
        out.update({
            "reason": reason,
            "minutes": int(t.credited // 60), "seconds": round(t.credited, 1),
            "extra_minutes": int(s.extra_seconds // 60),
            "idle_deducted": int(round(t.idle_lost / 60)),
            "pages": self.page_range(), "start_loc": self.start_loc, "end_loc": end_loc,
            # 出过但一次都没答的题（时间到了没答）不算进「小测」那一行
            "quizzes": [q for q in self.shown_quizzes if getattr(q, "attempts", None)],
            "quiz_results": list(self.results), "asked": asked, "highlights": hl,
            "exit_reason": s.session.exit_reason if s.session else None,
            "started_at": self.started_at, "ended_at": self._clock(),
            "today": s.daily(),
        })
        self._summary = out
        return out

    def _drop_overlays(self) -> None:
        if self.overlay_text is not None:
            self.overlay_text.detach()
        if self.overlays is not None:
            try:
                self.overlays.close()
            except Exception:  # noqa: BLE001
                pass
            self.overlays = None

    def _close_and_emit(self, summary: dict) -> None:
        self.dispose()
        self.finished.emit(summary)

    def dispose(self) -> None:
        """收摊：停表、撤事件过滤、断开 LeaveWatcher、撤遮罩、关窗口。不发 finished。"""
        if self._disposed:
            return
        self._disposed = True
        self.ended = True
        self._tick.stop()
        self._save_timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        watcher, self.watcher = self.watcher, None       # 收两次摊（先 finished 再 dispose）也不出警告
        if watcher is not None:
            try:
                watcher.left.disconnect(self._watch_left)
                watcher.returned.disconnect(self._watch_returned)
            except (RuntimeError, TypeError):
                pass
        self._drop_overlays()
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
        elif not self.locked:
            self.widget.close()
            self.widget.deleteLater()


# ── 演示 ─────────────────────────────────────────────────────
def _demo() -> None:
    """python -m reader.ui.reading [书的路径] [--light] [--minutes N] [--fast]

    普通窗口 1280×800，**不进锁屏**（不调 show_locked、不铺遮罩）。书页和 AI 栏用 stand_ins 里的替身
    （B、C 的真零件合进来后会自动换成真的）。--fast：发呆 20 秒、1 秒当 10 秒，好看各种状态。
    """
    import argparse
    import sys
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("book", nargs="?")
    ap.add_argument("--light", action="store_true")
    ap.add_argument("--minutes", type=int, default=27)
    ap.add_argument("--fast", action="store_true")
    args = ap.parse_args()

    theme.prepare_platform()
    app = QApplication(sys.argv)
    theme.load_fonts()
    theme.set_theme("light" if args.light else "dark", save=False)
    from . import stand_ins as si
    path = args.book or si.find_demo_book()
    if not path:
        print("~/Downloads 里没找到书，给个路径：python -m reader.ui.reading 书.epub")
        return
    tmp = Path(tempfile.mkdtemp(prefix="reader-demo-"))
    lib = si.MemoryLibrary()
    entry = lib.add(Path(path))
    clock = si.SpeedClock(10.0 if args.fast else 1.0)
    deps = si.demo_deps(clock=clock, state_path=tmp / "session.json", status_path=tmp / "status.json",
                        idle_timeout=20 if args.fast else 300)
    rs = ReadingScreen(entry, args.minutes, lib, deps=deps, locked=False)
    rs.widget.setWindowTitle("reader · 读书界面演示（没有锁屏）")

    def done(summary):
        print("finished:", {k: v for k, v in summary.items() if k not in ("today",)})
        app.quit()
    rs.finished.connect(done)
    rs.start()
    app.exec()


if __name__ == "__main__":
    _demo()
