"""书的统一模型：不管 PDF 还是 EPUB，对外都是「一串块 + 一串小节」。

## 位置

- EPUB：`Loc(doc, block)` = 第几个正文文件（spine 顺序）的第几个块（段落 / 标题 / 图）。
  界面按块重新排版（MiSans，不用书自带的 CSS），所以块号就是稳定的位置。
- PDF：`Loc(page, 0)` = 第几页。PDF 没法重排，按页记。

## 小节

小节是「读完就弹一道题」的单位（SPEC：读完一个小节问 1 题）。
优先用书的目录；目录太粗（一章几万字）就按段落边界再切；没有目录就每 10 页（PDF）/
每约 8000 字（EPUB）切一节。小节的 `text()` 就是喂给 AI 的正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, order=True)
class Loc:
    doc: int      # EPUB：spine 序号；PDF：页码（从 0 起）
    block: int = 0  # EPUB：块序号；PDF 恒为 0


@dataclass
class Block:
    kind: str          # "p" / "h1".."h6" / "img"
    text: str = ""     # 纯文本（img 为 alt）
    src: str = ""      # img 在 zip 里的路径


@dataclass
class Section:
    idx: int
    title: str
    start: Loc          # 含
    end: Loc            # 不含
    level: int = 1
    _text: Callable[[], str] | None = field(default=None, repr=False, compare=False)

    def text(self) -> str:
        return self._text() if self._text else ""

    def contains(self, loc: Loc) -> bool:
        return self.start <= loc < self.end


@dataclass
class TocEntry:
    title: str
    level: int
    loc: Loc


# 目录里的注释编号（「[12]」「(3)」「【7】」「12」）不是小节 —— 有些转制的电子书把脚注塞进了目录
_FOOTNOTE = re.compile(r"^[\[(（【〔]?\s*\d{1,4}\s*[\])）】〕]?$")


def is_footnote_title(title: str) -> bool:
    return bool(_FOOTNOTE.match(title.strip()))


# 不是正文的节（小测不出这些）：目录、版权页、封面、书名页……
_FRONT = re.compile(r"^(目\s*录|目\s*次|版\s*权.*|封\s*面|封\s*底|扉\s*页|书名页|contents?|table of contents|"
                    r"copyright.*|cover|title page)$", re.I)


def is_front_matter_title(title: str) -> bool:
    return bool(_FRONT.match(re.sub(r"[\s　·.。:：]+$", "", title.strip())))


class Book:
    """子类负责填 title / author / toc / sections，并实现 text_between。"""

    kind = ""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.title = self.path.stem
        self.author = ""
        self.toc: list[TocEntry] = []
        self.sections: list[Section] = []

    # 子类实现
    def text_between(self, start: Loc, end: Loc) -> str:
        raise NotImplementedError

    def section_at(self, loc: Loc) -> Section | None:
        for s in self.sections:
            if s.contains(loc):
                return s
        return self.sections[-1] if self.sections and loc >= self.sections[-1].start else None

    def place_title(self, loc: Loc) -> str:
        """这个位置在哪一章（给人看的「读到哪儿」）：目录里在它前面最近的一项；没有目录用小节名。

        小节是出题的单位，会把太短的节并进下一节（标题用后面那节的）、太长的切成「（2/3）」，
        拿来写「读到哪儿」会对不上 —— 读译后记时写着「附录一」。"""
        title, best = "", None
        for e in self.toc:      # 不假设目录按位置排好（PDF 的书签偶尔前后颠倒）；同一处的几项取后面那项
            if e.loc <= loc and (best is None or e.loc >= best):
                title, best = e.title, e.loc
        if not title:
            s = self.section_at(loc)
            title = s.title if s else ""
        return title

    def toc_outline(self, limit: int = 4000) -> str:
        """给 AI 看的目录（缩进表示层级），太长就截断。"""
        lines = [("  " * (e.level - 1)) + e.title for e in self.toc] or \
                [s.title for s in self.sections]
        out, n = [], 0
        for ln in lines:
            n += len(ln) + 1
            if n > limit:
                out.append("……（目录太长，后面省略）")
                break
            out.append(ln)
        return "\n".join(out)

    def _make_sections(self, bounds: list[tuple[str, int, Loc]], end: Loc) -> None:
        """bounds：(标题, 层级, 起点)，按位置排好。生成首尾相接的小节。"""
        secs = []
        for i, (title, level, start) in enumerate(bounds):
            stop = bounds[i + 1][2] if i + 1 < len(bounds) else end
            if stop <= start:
                continue
            s = Section(len(secs), title, start, stop, level)
            s._text = (lambda a=start, b=stop: self.text_between(a, b))
            secs.append(s)
        self.sections = secs


def open_book(path: str | Path) -> Book:
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".epub":
        from .epub import EpubBook
        return EpubBook(p)
    if ext == ".pdf":
        from .pdf import PdfBook
        return PdfBook(p)
    raise ValueError(f"不支持的格式：{ext}（只支持 PDF、EPUB）")
