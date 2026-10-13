"""选中文字后的小条（界面稿板 4）：「问 AI」「高亮」；点在已有高亮上变成「问 AI」「取消高亮」。

- 出在选区上方，小尖角指向选区开头；上面放不下就放到选区下方，尖角朝上。
- 是书页里的子部件，不另开窗口；按钮不拿键盘焦点（←→ 翻页照常）。
- 点别处就消失（书页负责叫 `dismiss()`）。Esc 也行。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from reader.ui import theme

from .base import icon, on_theme, shadow, swatch

ARROW = 6          # 尖角高度
ARROW_X = 27       # 尖角中心离小条左边多远（界面稿 left:22px + 半个 10px）


class SelectionPopover(QFrame):
    ask = Signal()
    highlight = Signal()
    unhighlight = Signal()

    def __init__(self, host):
        super().__init__(host)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self._below = False
        self._existing = False
        self.lay = QHBoxLayout(self)
        self.lay.setSpacing(2)
        self.b_ask = self._button("问 AI")
        self.b_hl = self._button("高亮")
        self.lay.addWidget(self.b_ask)
        self.lay.addWidget(self.b_hl)
        self.b_ask.setToolTip("把这段带到左边，问 AI")
        self.b_ask.clicked.connect(self.ask)
        self.b_hl.clicked.connect(self._hl_clicked)
        shadow(self, 30, 12)
        self._margins()
        self._restyle()
        on_theme(self, self._restyle)
        self.hide()

    def _button(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setFocusPolicy(Qt.NoFocus)
        b.setCursor(Qt.PointingHandCursor)
        b.setFont(theme.font(13))
        b.setIconSize(QSize(14, 14))
        theme.style(b, """
QPushButton {{ background: transparent; border: 0; color: {t1}; font-size: 13px;
  height: 32px; padding: 0 12px; border-radius: 7px; }}
QPushButton:hover {{ background: {bg5}; }}
QPushButton:pressed {{ background: {bg4}; }}""")
        return b

    def _restyle(self) -> None:
        self.b_ask.setIcon(icon("spark", "t1"))
        self.b_hl.setIcon(swatch("hl", 12, crossed=self._existing))
        self.update()

    def _margins(self) -> None:
        # 四周 4px；尖角那一边再加 ARROW
        if self._below:
            self.lay.setContentsMargins(4, 4 + ARROW, 4, 4)
        else:
            self.lay.setContentsMargins(4, 4, 4, 4 + ARROW)

    def _hl_clicked(self) -> None:
        (self.unhighlight if self._existing else self.highlight).emit()

    # ── 摆放 ─────────────────────────────────────────────
    def show_for(self, first: QRectF, last: QRectF, existing: bool = False) -> None:
        """first / last：选区第一行、最后一行的字框（父部件坐标）。"""
        self._existing = existing
        self.b_hl.setText("取消高亮" if existing else "高亮")
        self.b_hl.setToolTip("去掉这处高亮" if existing else "标黄，下次打开还在")
        host = self.parentWidget()
        self._below = first.top() - 16 - 44 < 0
        self._margins()
        self._restyle()
        self.adjustSize()
        w, h = self.width(), self.height()
        if self._below:
            ax, y = last.right(), last.bottom() + 12
        else:
            ax, y = first.left(), first.top() - 16 + ARROW - h
        x = round(ax - ARROW_X + 13)   # 尖角落在选区开头往里一点（界面稿 left:-14px）
        x = max(6, min(x, host.width() - w - 6))
        self._arrow_x = max(14, min(ax - x, w - 14))
        self.move(x, max(4, round(y)))
        self.show()
        self.raise_()

    def dismiss(self) -> None:
        self.hide()

    # ── 画：圆角框 + 尖角 ─────────────────────────────────────
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if self._below:
            box = QRectF(0.5, ARROW + 0.5, w - 1, h - ARROW - 1)
        else:
            box = QRectF(0.5, 0.5, w - 1, h - ARROW - 1)
        path = QPainterPath()
        path.addRoundedRect(box, 10, 10)
        ax = getattr(self, "_arrow_x", ARROW_X)
        tri = QPainterPath()
        if self._below:
            tri.moveTo(ax - ARROW, box.top() + 0.5)
            tri.lineTo(ax, 0.5)
            tri.lineTo(ax + ARROW, box.top() + 0.5)
        else:
            tri.moveTo(ax - ARROW, box.bottom() - 0.5)
            tri.lineTo(ax, h - 0.5)
            tri.lineTo(ax + ARROW, box.bottom() - 0.5)
        tri.closeSubpath()
        path = path.united(tri)
        p.setBrush(theme.color("bg3"))
        p.setPen(QPen(theme.color("l2"), 1))
        p.drawPath(path)


if __name__ == "__main__":
    # 演示：打开真书，自动选中一句，小条出现在选区上方（F2 换主题）
    import sys

    from .base import demo_main
    sys.exit(demo_main("epub", "~/Downloads/*.epub", sys.argv[1:],
                       after=lambda view: view.demo_select()))
