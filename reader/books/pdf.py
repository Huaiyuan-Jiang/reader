"""PDF：PyMuPDF 取目录和每页文字。位置按页记（Loc(page, 0)）。

扫描版（没有文字层，`has_text` 为 False）：用 Windows 自带的文字识别（books/ocr.py）补上。
识别结果存在本地，`page_text` / `page_words` 自动用它 —— AI 读得到、能出题、能选字划线。
还没识别到的页当没有字；`text_source` 告诉界面现在的情况（"text" / "ocr" / "none"）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pymupdf

from .model import Book, Loc, TocEntry, is_footnote_title

PAGES_PER_SECTION = 10   # 没有目录时每 10 页一节（SPEC）
SEC_MIN_CHARS = 1500     # 目录里太短的小节并进下一节
SEC_MAX_PAGES = 15       # 超过这么多页的小节按 10 页一份切开


class PdfBook(Book):
    kind = "pdf"

    def __init__(self, path: Path):
        super().__init__(path)
        self.doc = pymupdf.open(self.path)
        if self.doc.needs_pass:
            raise ValueError("这个 PDF 有打开密码，读不了")
        self.pages = self.doc.page_count
        meta = self.doc.metadata or {}
        # 很多 PDF 的 title 元数据是 Word 模板留下的无关标题，文件名反而更可靠：
        # 元数据标题只在文件名看起来是乱码 / 纯数字时才用
        if meta.get("title") and self.title.replace("_", "").isdigit():
            self.title = meta["title"]
        self.author = meta.get("author", "") or ""
        self.toc = [TocEntry(t.strip(), max(1, lv), Loc(max(0, p - 1)))
                    for lv, t, p, *_ in self.doc.get_toc(simple=True) if t.strip() and p >= 1 and not is_footnote_title(t)]
        sample = range(min(self.pages, 12))
        self.has_text = sum(len(self._native_text(i)) for i in sample) > 100 * max(1, len(sample)) // 4
        self.ocr = None
        if not self.has_text:
            from . import ocr
            if ocr.available():
                self.ocr = ocr.OcrStore(self.path)
        self._build_sections()

    @property
    def text_source(self) -> str:
        """"text" = 自带文字层；"ocr" = 扫描版，已经（部分）识别；"none" = 扫描版，还没识别 / 识别不了。"""
        if self.has_text:
            return "text"
        return "ocr" if self.ocr is not None and self.ocr.done_pages() else "none"

    def ocr_progress(self) -> tuple[int, int]:
        return (self.ocr.done_pages() if self.ocr else 0), self.pages

    def ocr_running(self) -> bool:
        """后台现在是不是真的在识别这本（书架的 OcrQueue 可能在做别的书，或者已经停了）。"""
        return self.ocr is not None and self.ocr.running()

    @lru_cache(maxsize=4096)
    def _native_text(self, page: int) -> str:
        if not 0 <= page < self.pages:
            return ""
        return self.doc[page].get_text("text").strip()

    def page_text(self, page: int) -> str:
        t = self._native_text(page)
        if t or self.ocr is None:
            return t
        r = self.ocr.page(page)
        return (r or {}).get("text", "")

    def page_words(self, page: int) -> list[tuple[float, float, float, float, str, int]]:
        """这一页的词和位置（PDF 坐标）：(x0, y0, x1, y1, 词, 行号)。选字、划线、找出处用。

        文字层有就用文字层（pymupdf 的 words），扫描版用识别结果；都没有返回 []（这页不能选字）。
        """
        if not 0 <= page < self.pages:
            return []
        if self.has_text:
            return [(w[0], w[1], w[2], w[3], w[4], w[5] * 1000 + w[6])
                    for w in self.doc[page].get_text("words")]
        r = self.ocr.page(page) if self.ocr else None
        return [tuple(w) for w in (r or {}).get("words", [])]

    def page_png(self, page: int, width_px: int = 1600) -> bytes:
        """这一页的图片（发给 Claude 看图表用）。"""
        import pymupdf
        pg = self.doc[page]
        s = width_px / pg.rect.width
        return pg.get_pixmap(matrix=pymupdf.Matrix(s, s)).tobytes("png")

    def end(self) -> Loc:
        return Loc(self.pages, 0)

    def text_between(self, start: Loc, end: Loc) -> str:
        parts = []
        for p in range(start.doc, min(end.doc, self.pages)):
            t = self.page_text(p)
            if t:
                parts.append(f"〔第 {p + 1} 页〕\n{t}")
        return "\n\n".join(parts)

    def _chars(self, a: int, b: int) -> int:
        return sum(len(self.page_text(p)) for p in range(a, min(b, self.pages)))

    def _build_sections(self) -> None:
        end = self.end()
        pts: dict[int, tuple[str, int]] = {}
        for e in self.toc:
            pts.setdefault(e.loc.doc, (e.title, e.level))   # 同一页多个目录项：取第一个
        if pts:
            bounds = [(t, lv, p) for p, (t, lv) in sorted(pts.items())]
            if bounds[0][2] > 0:
                bounds.insert(0, ("开头", 1, 0))
            # 太短的并进下一节
            merged = []
            for i, (t, lv, p) in enumerate(bounds):
                stop = bounds[i + 1][2] if i + 1 < len(bounds) else self.pages
                if merged and merged[-1][3] < SEC_MIN_CHARS:
                    _, _, mp, n = merged.pop()
                    merged.append((t, lv, mp, n + self._chars(p, stop)))
                else:
                    merged.append((t, lv, p, self._chars(p, stop)))
            if len(merged) > 1 and merged[-1][3] < SEC_MIN_CHARS:
                merged.pop()
            bounds = [(t, lv, p) for t, lv, p, _ in merged]
        else:
            bounds = [(f"第 {p + 1}–{min(p + PAGES_PER_SECTION, self.pages)} 页", 1, p)
                      for p in range(0, self.pages, PAGES_PER_SECTION)]
        # 太长的按 10 页切
        out = []
        for i, (t, lv, p) in enumerate(bounds):
            stop = bounds[i + 1][2] if i + 1 < len(bounds) else self.pages
            if stop - p > SEC_MAX_PAGES:
                starts = list(range(p, stop, PAGES_PER_SECTION))
                if len(starts) > 1 and stop - starts[-1] < PAGES_PER_SECTION // 2:
                    starts.pop()   # 最后一份太薄就并进前一份
                for k, s in enumerate(starts):
                    out.append((f"{t}（{k + 1}/{len(starts)}）", lv, Loc(s)))
            else:
                out.append((t, lv, Loc(p)))
        self._make_sections(out, end)
