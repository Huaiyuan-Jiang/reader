"""PDF 出图的工作循环（PdfView 用）。默认在子进程里跑。

为什么用子进程：PyMuPDF 出图时一直占着 Python 的全局锁（实测一页 15–80ms，复杂页更久），
放在线程里，界面线程会跟着卡，滚动就不顺。子进程自己有一把锁，界面线程完全不受影响。
子进程起不来（比如打包后的环境）就退回线程，照样能用，只是滚动时偶尔顿一下。

这个模块故意不导入 Qt：子进程只要 pymupdf 和 numpy。

协议（主进程 → 工作循环）：
    ("open", 路径)                      换一本书
    ("want", [(页, 宽像素, 颜色), ...])  现在想要的页，按先后排；新的一条整个替换旧的
    ("quit",)
工作循环 → 主进程：
    ("img", 页, 宽像素, 颜色, 宽, 高, BGRA 字节)  —— Qt 的 Format_RGB32
    ("err", 页, 宽像素, 颜色, 说明)
颜色：None = 原样；("tint", 黑换成, 白换成)；("invert", 黑换成, 白换成) = 先反色再上色。颜色都是 0xRRGGBB。
"""

from __future__ import annotations


def render(doc, page: int, px_w: int, colors) -> tuple[int, int, bytes]:
    import numpy as np
    import pymupdf
    pg = doc[page]
    z = px_w / max(1.0, pg.rect.width)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False, colorspace=pymupdf.csRGB)
    if colors:
        kind, black, white = colors
        if kind == "invert":
            pix.invert_irect(pix.irect)
        pix.tint_with(black, white)
    w, h = pix.width, pix.height
    a = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(h, pix.stride)[:, : w * 3].reshape(h, w, 3)
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., 0] = a[..., 2]
    out[..., 1] = a[..., 1]
    out[..., 2] = a[..., 0]
    out[..., 3] = 255
    return w, h, out.tobytes()


def worker(path: str, req, res) -> None:
    """req / res：有 recv / poll / send 的连接（multiprocessing 的 Pipe 或线程版的替身）。"""
    import pymupdf
    doc = pymupdf.open(path) if path else None
    queue: list = []

    def handle(msg) -> bool:
        nonlocal doc, queue
        if msg[0] == "quit":
            return False
        if msg[0] == "open":
            doc = pymupdf.open(msg[1])
            queue = []
        elif msg[0] == "want":
            queue = list(msg[1])
        return True

    while True:
        try:
            if not queue and not handle(req.recv()):
                return
            while req.poll():
                if not handle(req.recv()):
                    return
        except (EOFError, OSError, BrokenPipeError):
            return
        if not queue or doc is None:
            continue
        page, px_w, colors = queue.pop(0)
        try:
            w, h, data = render(doc, page, px_w, colors)
            res.send(("img", page, px_w, colors, w, h, data))
        except (EOFError, OSError, BrokenPipeError):
            return
        except Exception as e:  # noqa: BLE001 —— 一页坏了不影响别的页
            try:
                res.send(("err", page, px_w, colors, f"{type(e).__name__}: {e}"))
            except (EOFError, OSError):
                return


if __name__ == "__main__":
    # 演示：用真书出一页「暖纸」图，存成 PNG，报时间。python -m reader.ui.bookview._render [PDF 路径] [页码]
    import glob
    import os
    import sys
    import tempfile
    import time

    import numpy as np
    import pymupdf

    from reader.ui import theme   # 只有演示用得着颜色；工作循环本身不碰 Qt

    def rgb(key: str) -> int:
        return int(theme.tok(key, "dark").lstrip("#"), 16)

    hits = sorted(glob.glob(os.path.expanduser("~/Downloads/*.pdf")))
    path = sys.argv[1] if len(sys.argv) > 1 else (hits[0] if hits else "")
    if not path:
        sys.exit("~/Downloads 里没找到 PDF，请把路径写在命令后面")
    page = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    doc = pymupdf.open(path)
    t = time.perf_counter()
    w, h, data = render(doc, page, 1290, ("tint", rgb("pdfink"), rgb("pdfpaper")))
    ms = 1000 * (time.perf_counter() - t)
    bgra = np.frombuffer(data, np.uint8).reshape(h, w, 4)
    rgb_bytes = np.ascontiguousarray(bgra[..., 2::-1]).tobytes()
    out = os.path.join(tempfile.gettempdir(), "reader-render-demo.png")
    pymupdf.Pixmap(pymupdf.csRGB, w, h, rgb_bytes, False).save(out)
    print(f"第 {page + 1} 页：{w}×{h}，暖纸，出图 {ms:.0f}ms → {out}")
