"""书页：EPUB 用 Qt 自己排（EpubView），PDF 用 PyMuPDF 出图（PdfView）。接口见 base.BookView。

这里不在导入时加载 Qt 部件：PDF 出图的子进程只需要 `_render`，不该把整套界面拖进去。
"""

from __future__ import annotations

__all__ = ["BookView", "EpubView", "PdfView", "create_view"]


def __getattr__(name):
    if name == "BookView":
        from .base import BookView
        return BookView
    if name == "EpubView":
        from .epub_view import EpubView
        return EpubView
    if name == "PdfView":
        from .pdf_view import PdfView
        return PdfView
    raise AttributeError(name)


def create_view(book, parent=None):
    """按书的类型给一个书页部件（还没 open）。"""
    if book.kind == "epub":
        from .epub_view import EpubView
        return EpubView(parent)
    from .pdf_view import PdfView
    return PdfView(parent)
