"""副屏遮罩上的字（界面稿板 9、板 7③）。

遮罩窗口本身是 reader.lock.windows.OverlayManager（深色、置顶、跟着屏幕插拔）；这里只管写什么：

    正在读书                         阅读暂停了
    还剩 16 分钟                     还剩 16 分钟
                                     回主屏点一下阅读窗口，就接着读
    读完会自动撤掉                   这次离开已记下

- **一分钟变一次，不跳秒**：OverlayText.update() 每秒调都行，只有「分钟数或状态」变了才真的去改字。
- 暂停（发呆 / 离开）时换成「阅读暂停了」，数字暗一点。
- 两种主题都是深色遮罩，颜色用 theme 的 mon / mon_t / mon_n。
- 没有按钮，点它什么都不做（OverlayManager 那边就是一个 QLabel）。

OverlayManager.set_text() 收的是 QLabel 文字，QLabel 认得出 HTML，所以这里直接给排好的 HTML。
"""

from __future__ import annotations

from . import theme
from .strip import minutes_left

READING, IDLE, AWAY = "reading", "idle", "away"


def _spaced(text: str) -> str:
    """「正在读书」字距拉开一点（Qt 富文本不认 letter-spacing，用细空格）。"""
    return " ".join(text)


def overlay_lines(state: str, remaining_seconds: float) -> dict:
    """遮罩上要写的几行（纯文字，方便单测）。"""
    n = minutes_left(remaining_seconds)
    if state == READING:
        return {"title": "正在读书", "minutes": n, "hint": "", "foot": "读完会自动撤掉"}
    if state == AWAY:
        return {"title": "阅读暂停了", "minutes": n, "hint": "回主屏点一下阅读窗口，就接着读",
                "foot": "这次离开已记下"}
    return {"title": "阅读暂停了", "minutes": n, "hint": "回主屏翻一页，就接着读", "foot": "计时先停了"}


def overlay_html(state: str, remaining_seconds: float) -> str:
    d = overlay_lines(state, remaining_seconds)
    fam = theme.family()
    t = theme.tok("mon_t")
    num = theme.tok("mon_n") if state == READING else theme.mix("mon_n", "mon", 0.7)
    foot = theme.mix("mon_t", "mon", 0.7)
    hint = (f'<p align="center" style="margin-top:14px;font-size:14px;color:{t}">{d["hint"]}</p>'
            if d["hint"] else "")
    return (
        f'<div style="font-family:\'{fam}\';">'
        f'<p align="center" style="margin:0;font-size:22px;color:{t}">{_spaced(d["title"])}</p>'
        f'<p align="center" style="margin-top:6px;font-size:15px;color:{t}">还剩&nbsp;'
        f'<span style="font-size:64px;font-weight:600;color:{num}">{d["minutes"]}</span>&nbsp;分钟</p>'
        f'{hint}'
        f'<p align="center" style="margin-top:{56 if hint else 90}px;font-size:12px;color:{foot}">'
        f'{d["foot"]}</p></div>'
    )


class OverlayText:
    """把状态和剩余时间推给遮罩；只在分钟数或状态变了时才改字。

    target 是有 set_text(str) 的东西（OverlayManager，测试里可以是假的）。
    """

    def __init__(self, target):
        self.target = target
        self._last: tuple | None = None
        self.pushes = 0
        theme.on_change(lambda _n: self._repush())

    def update(self, state: str, remaining_seconds: float) -> bool:
        key = (state, minutes_left(remaining_seconds))
        if key == self._last:
            return False
        self._last = key
        self._push(state, remaining_seconds)
        return True

    def _push(self, state: str, remaining_seconds: float) -> None:
        if self.target is None:
            return
        self.pushes += 1
        self.target.set_text(overlay_html(state, remaining_seconds))

    def _repush(self) -> None:
        if self._last is not None:
            st, m = self._last
            self._push(st, m * 60)

    def detach(self) -> None:
        """遮罩撤了（时间到 / 结束），以后不再推字。"""
        self.target = None
