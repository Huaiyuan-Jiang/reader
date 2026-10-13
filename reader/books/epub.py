"""EPUB：自己拆 zip，按 spine 顺序把每个正文文件拆成块。

不用书自带的 CSS 排版（界面统一用 MiSans 重排，见 SPEC「书页渲染」），
所以这里只要结构：段落、标题、图片，以及 id → 块号（目录锚点要用）。
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from .model import Block, Book, Loc, TocEntry, is_footnote_title

# 小节长度（字符）：短于 MIN 并进下一节，长于 MAX 按段落切成约 TARGET 一份
SEC_MIN = 1500
SEC_MAX = 12000
SEC_TARGET = 8000

_BLOCK_TAGS = {"p", "div", "li", "blockquote", "pre", "dd", "dt", "td", "caption", "figcaption",
               "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "tr"}
_SKIP_TAGS = {"script", "style", "head", "title", "svg", "math"}


class _Blocks(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.blocks: list[Block] = []
        self.ids: dict[str, int] = {}
        self._buf: list[str] = []
        self._kind = "p"
        self._skip = 0
        self._pending_ids: list[str] = []

    def _flush(self):
        text = re.sub(r"[ \t\r\n　]+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            for i in self._pending_ids:
                self.ids.setdefault(i, len(self.blocks))
            self._pending_ids = []
            self.blocks.append(Block(self._kind, text))
        self._kind = "p"

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if "id" in a:
            self._pending_ids.append(a["id"])
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag[0] == "h" and tag[1:].isdigit():
                self._kind = tag
        elif tag == "br":
            self._buf.append("\n")
        elif tag in ("img", "image"):
            src = a.get("src") or a.get("xlink:href") or a.get("href") or ""
            if src:
                self._flush()
                for i in self._pending_ids:
                    self.ids.setdefault(i, len(self.blocks))
                self._pending_ids = []
                path = posixpath.normpath(posixpath.join(self.base, unquote(src)))
                self.blocks.append(Block("img", a.get("alt", ""), path))
        elif tag == "a" and "name" in a:
            self._pending_ids.append(a["name"])

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if not self._skip and tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()
        # 文件末尾的锚点指向最后
        for i in self._pending_ids:
            self.ids.setdefault(i, len(self.blocks))


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag) or re.search(rf"\b{name}\s*=\s*'([^']*)'", tag)
    return unescape(m.group(1)) if m else ""


class EpubBook(Book):
    kind = "epub"

    def __init__(self, path: Path):
        super().__init__(path)
        self.zip = zipfile.ZipFile(self.path)
        self.docs: list[str] = []            # spine 里每个文件在 zip 中的路径
        self.blocks: list[list[Block]] = []  # 每个文件的块
        self._ids: list[dict[str, int]] = []
        self._load()

    # ── 读取 ─────────────────────────────────────────────
    def _read(self, name: str) -> str:
        raw = self.zip.read(name)
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "ignore")

    def _load(self) -> None:
        names = set(self.zip.namelist())
        opf = ""
        if "META-INF/container.xml" in names:
            m = re.search(r'full-path\s*=\s*"([^"]+)"', self._read("META-INF/container.xml"))
            opf = m.group(1) if m else ""
        if opf not in names:
            opf = next((n for n in names if n.lower().endswith(".opf")), "")
        if not opf:
            raise ValueError("这个 EPUB 找不到 .opf，文件可能坏了")
        base = posixpath.dirname(opf)
        s = self._read(opf)

        t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", s, re.S)
        if t and t.group(1).strip():
            self.title = unescape(t.group(1).strip())
        a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", s, re.S)
        self.author = unescape(a.group(1).strip()) if a else ""

        manifest: dict[str, tuple[str, str, str]] = {}
        for tag in re.findall(r"<item\b[^>]*>", s):
            iid = _attr(tag, "id")
            href = posixpath.normpath(posixpath.join(base, unquote(_attr(tag, "href"))))
            manifest[iid] = (href, _attr(tag, "media-type"), _attr(tag, "properties"))
        spine = [_attr(t, "idref") for t in re.findall(r"<itemref\b[^>]*>", s)
                 if _attr(t, "linear") != "no"]
        for iid in spine:
            href, mt, _ = manifest.get(iid, ("", "", ""))
            if href in names and ("html" in mt or href.lower().endswith((".html", ".xhtml", ".htm"))):
                p = _Blocks(posixpath.dirname(href))
                p.feed(self._read(href))
                p.close()
                self.docs.append(href)
                self.blocks.append(p.blocks)
                self._ids.append(p.ids)

        nav = next((h for h, _, props in manifest.values() if "nav" in props.split()), "")
        ncx = next((h for h, mt, _ in manifest.values() if mt == "application/x-dtbncx+xml"), "")
        if nav in names:
            self.toc = self._toc_nav(nav)
        if not self.toc and ncx in names:
            self.toc = self._toc_ncx(ncx)
        self.toc = [e for e in self.toc if not is_footnote_title(e.title)]
        self.toc = self._repair_toc(self.toc)
        self._build_sections()

    # ── 目录纠错 ──────────────────────────────────────────
    # 有些转制的书目录文件是错的（比如「第一章」指到序言中间、「译后记」指到正文某一章），
    # 小节、顶条、小测卡的节名都跟着错。这里拿正文里的标题核对：目录项指的地方附近有这个标题就信它；
    # 对不上就到全书的标题里找同名的那一个；找不到、位置又跟前后对不上的，扔掉。最后按位置排好。
    _NOTE_MARK = re.compile(r"[\[(（【〔]\s*\d{1,3}\s*[\])）】〕]")

    @classmethod
    def _key(cls, text: str) -> str:
        """比较标题用：去掉注释编号，只留字母和数字（汉字也算字母），小写。"""
        s = cls._NOTE_MARK.sub("", text)
        return "".join(c.lower() for c in s if c.isalnum())

    @staticmethod
    def _same_title(a: str, b: str) -> bool:
        if not a or not b:
            return False
        if a == b:
            return True
        short, long_ = sorted((a, b), key=len)
        return len(short) >= 2 and long_.startswith(short) and len(short) >= 0.5 * len(long_)

    def _heading_at(self, loc: Loc, key: str) -> bool:
        """目录项指的地方（或往后两块之内，锚点常落在标题前一个空元素上）是不是这个标题。"""
        if not 0 <= loc.doc < len(self.blocks):
            return False
        blocks = self.blocks[loc.doc]
        for b in range(loc.block, min(loc.block + 3, len(blocks))):
            k = self._key(blocks[b].text)
            if self._same_title(key, k) or (len(key) >= 2 and len(k) <= 3 * len(key) and key in k):
                return True
        return False

    def _repair_toc(self, toc: list[TocEntry]) -> list[TocEntry]:
        if not toc or not self.blocks:
            return toc
        # 全书的「标题」：h1–h6，以及每个文件第一块（有的书章名用 <p> 加样式）里短的那种
        heads: list[tuple[Loc, str]] = []
        for d, blocks in enumerate(self.blocks):
            for b, blk in enumerate(blocks):
                if blk.kind.startswith("h") or (b == 0 and blk.kind == "p" and len(blk.text) <= 40):
                    k = self._key(blk.text)
                    if k:
                        heads.append((Loc(d, b), k))
        fixed: list[tuple[TocEntry, bool]] = []
        last = Loc(0, 0)
        for e in toc:
            key = self._key(e.title)
            if self._heading_at(e.loc, key):
                fixed.append((e, True))
                last = e.loc
                continue
            hits = [loc for loc, k in heads if self._same_title(key, k)]
            if hits:
                loc = next((h for h in hits if h >= last), hits[0])   # 同名的（「注释」）取前一项后面最近的
                fixed.append((TocEntry(e.title, e.level, loc), True))
                last = loc
            else:
                fixed.append((e, False))   # 「书名页」「版权页」这种正文里没有同名标题的，先留着
        # 对不上的：夹在前后核对过的两项之间才留（顺序说得通），不然多半也是指错了的
        out: list[TocEntry] = []
        for i, (e, ok) in enumerate(fixed):
            if not ok:
                prev = next((f.loc for f, k in reversed(fixed[:i]) if k), None)
                nxt = next((f.loc for f, k in fixed[i + 1:] if k), None)
                if (prev is not None and e.loc < prev) or (nxt is not None and e.loc > nxt):
                    continue
            out.append(e)
        out.sort(key=lambda e: e.loc)   # sort 是稳定的：同一处的几项保持原来的先后
        return out

    def _resolve(self, href_base: str, href: str) -> Loc | None:
        path, _, frag = unquote(href).partition("#")
        full = posixpath.normpath(posixpath.join(href_base, path)) if path else ""
        if full not in self.docs:
            return None
        d = self.docs.index(full)
        return Loc(d, self._ids[d].get(frag, 0) if frag else 0)

    def _toc_ncx(self, ncx: str) -> list[TocEntry]:
        s = self._read(ncx)
        base = posixpath.dirname(ncx)
        out: list[TocEntry] = []
        depth = 0
        for m in re.finditer(r"<navPoint\b|</navPoint>|<text>(.*?)</text>|<content\b[^>]*>", s, re.S):
            tok = m.group(0)
            if tok.startswith("<navPoint"):
                depth += 1
                title = None
            elif tok == "</navPoint>":
                depth -= 1
            elif tok.startswith("<text>"):
                title = unescape(re.sub(r"\s+", " ", m.group(1)).strip())
            else:
                loc = self._resolve(base, _attr(tok, "src"))
                if loc is not None and title:
                    out.append(TocEntry(title, max(1, depth), loc))
                    title = None
        return out

    def _toc_nav(self, nav: str) -> list[TocEntry]:
        s = self._read(nav)
        m = re.search(r'<nav\b[^>]*epub:type\s*=\s*"toc"[^>]*>(.*?)</nav>', s, re.S)
        body = m.group(1) if m else s
        base = posixpath.dirname(nav)
        out: list[TocEntry] = []
        depth = 0
        for m in re.finditer(r"<ol\b[^>]*>|</ol>|<a\b[^>]*>(.*?)</a>", body, re.S):
            tok = m.group(0)
            if tok.startswith("<ol"):
                depth += 1
            elif tok == "</ol>":
                depth -= 1
            else:
                title = unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(1)).strip())
                loc = self._resolve(base, _attr(tok, "href"))
                if loc is not None and title:
                    out.append(TocEntry(title, max(1, depth), loc))
        return out

    # ── 正文 ─────────────────────────────────────────────
    def end(self) -> Loc:
        return Loc(len(self.docs), 0)

    def iter_blocks(self, start: Loc, end: Loc):
        for d in range(start.doc, min(end.doc + 1, len(self.docs))):
            lo = start.block if d == start.doc else 0
            hi = end.block if d == end.doc else len(self.blocks[d])
            for b in range(lo, hi):
                yield Loc(d, b), self.blocks[d][b]

    def text_between(self, start: Loc, end: Loc) -> str:
        out = []
        for _, b in self.iter_blocks(start, end):
            if b.kind == "img":
                if b.text:
                    out.append(f"［图：{b.text}］")
            elif b.kind.startswith("h"):
                out.append(f"\n{b.text}\n")
            else:
                out.append(b.text)
        return "\n".join(out).strip()

    def _len(self, start: Loc, end: Loc) -> int:
        return sum(len(b.text) for _, b in self.iter_blocks(start, end) if b.kind != "img")

    def _build_sections(self) -> None:
        end = self.end()
        if self.toc:
            pts = sorted({e.loc: e for e in self.toc}.values(), key=lambda e: e.loc)
            bounds = [(e.title, e.level, e.loc) for e in pts]
            if bounds[0][2] > Loc(0, 0):
                bounds.insert(0, ("开头", 1, Loc(0, 0)))
        else:
            bounds = []
            for d, blocks in enumerate(self.docs):
                head = next((b.text for b in self.blocks[d] if b.kind.startswith("h")), "")
                bounds.append((head or f"第 {d + 1} 部分", 1, Loc(d, 0)))
        if not bounds:
            return
        bounds = self._merge_small(bounds, end)
        bounds = self._split_big(bounds, end)
        self._make_sections(bounds, end)

    def _merge_small(self, bounds, end):
        out = []
        for i, b in enumerate(bounds):
            stop = bounds[i + 1][2] if i + 1 < len(bounds) else end
            if out and out[-1][3] < SEC_MIN:
                # 上一节太短（多半是扉页、部名页）：并进这一节，起点用上一节的，标题用这一节的
                _, _, st, n = out.pop()
                out.append((b[0], b[1], st, n + self._len(b[2], stop)))
            else:
                out.append((b[0], b[1], b[2], self._len(b[2], stop)))
        if len(out) > 1 and out[-1][3] < SEC_MIN:
            out.pop()  # 最后一节太短：并进前一节（前一节的终点自动延到结尾）
        return [(t, lv, st) for t, lv, st, _ in out]

    def _split_big(self, bounds, end):
        out = []
        for i, (title, level, start) in enumerate(bounds):
            stop = bounds[i + 1][2] if i + 1 < len(bounds) else end
            total = self._len(start, stop)
            if total <= SEC_MAX:
                out.append((title, level, start))
                continue
            parts = max(2, round(total / SEC_TARGET))
            size = total / parts
            cuts, acc = [start], 0
            for loc, b in self.iter_blocks(start, stop):
                if acc >= size * len(cuts) and loc > cuts[-1] and len(cuts) < parts:
                    cuts.append(loc)
                acc += len(b.text) if b.kind != "img" else 0
            for k, c in enumerate(cuts):
                out.append((f"{title}（{k + 1}/{len(cuts)}）", level, c))
        return out

    def image_bytes(self, src: str) -> bytes | None:
        try:
            return self.zip.read(src)
        except KeyError:
            return None
