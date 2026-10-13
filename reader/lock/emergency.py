"""紧急出口的状态机 —— 只管逻辑，不画界面。

要连续确认 N 次（默认 3）才放行，中途任何一次取消就清零重来。每一步给界面一个不同的
``prompt_id``，界面据此换措辞、换按钮位置，防止「啪啪啪连点三下」直接冲出去。

用法：
    ee = EmergencyExit(required=3)
    pid = ee.begin()            # 显示第 pid 步的措辞
    done = ee.confirm()         # 主人点了确认；done=True 表示三次凑齐，可以走
    ee.cancel()                 # 主人改主意，清零
纯逻辑，可注入、可单测；真正记录 emergency_exit 由 LockSession 做。

原因（界面稿板 8 第 2 步「是什么急事？」）：``set_reason()`` 记下，跟这次退出的记录放在一起；
取消或重新开始就清掉。
"""

from __future__ import annotations


class EmergencyExit:
    def __init__(self, required: int = 3, prompt_ids: list[str] | None = None):
        if required < 1:
            raise ValueError("required 至少为 1")
        self.required = required
        # 每一步一个不同的 prompt_id；给少了就自动补成 ee-1 / ee-2 …
        ids = list(prompt_ids or [])
        while len(ids) < required:
            ids.append(f"ee-{len(ids) + 1}")
        self.prompt_ids = ids
        self.count = 0
        self.reason: str | None = None

    @property
    def active(self) -> bool:
        return self.count > 0

    @property
    def done(self) -> bool:
        return self.count >= self.required

    def remaining(self) -> int:
        return max(0, self.required - self.count)

    def current_prompt(self) -> str:
        """当前该显示哪一步的措辞。"""
        idx = min(self.count, self.required - 1)
        return self.prompt_ids[idx]

    def begin(self) -> str:
        """开始一轮紧急退出，回到第一步，返回第一步的 prompt_id。"""
        self.count = 0
        self.reason = None
        return self.current_prompt()

    def confirm(self) -> bool:
        """确认一次。凑齐 required 次返回 True（可以退出），否则 False。"""
        if self.done:
            return True
        self.count += 1
        return self.done

    def set_reason(self, reason: str | None) -> None:
        r = (reason or "").strip()
        self.reason = r or None

    def cancel(self) -> None:
        self.count = 0
        self.reason = None
