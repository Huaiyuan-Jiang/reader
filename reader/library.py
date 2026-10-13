"""书架的数据：有哪些书、哪本在读、读到哪、高亮、答完的小测、读书笔记、每次读书的记录。不带界面。

## 存在哪
都在 `%LOCALAPPDATA%\\reader\\`（环境变量 READER_DATA_DIR 可以换，测试用）：

- `library.json`：书的清单 + 「在读」是哪本 + 每次读书的记录 + 几个小设置。
- `books\\<id>.json`：每本书一个小文件，放目录（给书架显示「读到哪一节」）、各文件的块数（算百分比）、高亮。
  拆开是因为高亮和目录可能很长，翻一页就存一次位置时不想每次都重写一大份。
- `covers\\<id>.png`：封面（加书时顺手取：EPUB 里的封面图、PDF 第一页）。
- `笔记\\<书名>\\<日期> 第 N 次.md`：读书笔记。

所有 JSON 都是原子写（临时文件 + fsync + os.replace），跟 reader/lock/state.py 同一个办法。

## 书是「引用」不是「复制」
存原文件的绝对路径。文件挪走 / 删掉了，`BookEntry.missing` 为 True，书架上标出来，可以 `relink` 重新指过去。
从书架拿掉（remove）只是挪到「拿掉的」里：同一个文件再加回来，读到哪、高亮、答过的题都还在。

## 线程
`add()` 要打开整本书（大 PDF 几秒），界面要在工作线程里调；其余都很快。所有读写都上了锁。
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from reader.books import Loc

LIB_FILE = "library.json"
NOTES_DIR = "笔记"
BOOK_EXTS = (".pdf", ".epub")


def default_root() -> Path:
    """跟 theme.data_dir() 同一个位置（这里不引 theme，免得数据层带上 Qt）。"""
    return Path(os.environ.get("READER_DATA_DIR") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "reader")


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")   # 排「最近读过」要分得出先后


def book_id_for(path: str | Path) -> str:
    """同一个文件永远是同一个 id（大小写、斜杠方向不同也算同一个）。"""
    norm = os.path.normcase(os.path.abspath(str(path)))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def safe_name(s: str, limit: int = 60) -> str:
    """能当文件夹名的书名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip(" .")
    return (s[:limit].rstrip(" .") or "未命名")


# 有些下载站给的文件名：「书名 -- 作者 -- 出版社, 年 -- isbn -- md5 -- 站名」
_DASHES = re.compile(r"\s+--\s+")


def clean_title(title: str, stem: str) -> tuple[str, str]:
    """(书名, 从文件名猜的作者)。书自带的标题就是文件名时，把文件名里的附加信息去掉。"""
    title = (title or "").strip()
    guess_author = ""
    if not title or title == stem:
        parts = _DASHES.split(stem)
        if len(parts) >= 3:
            title, guess_author = parts[0].strip(), parts[1].strip()
        else:
            title = stem
    # 文件名里的「_」原本多半是冒号（文件名里不能有冒号）：中文之间换成「：」，后面跟空格的换成「: 」
    title = re.sub(r"(?<=[㐀-鿿])_(?=[㐀-鿿])", "：", title)
    title = re.sub(r"_\s", ": ", title).replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip() or stem.strip() or "未命名"
    return title, guess_author


@dataclass
class BookEntry:
    id: str
    path: str
    title: str
    author: str
    kind: str                  # "pdf" / "epub"
    position: Loc | None
    added: str
    last_read: str | None
    scanned: bool              # PDF 没有文字层
    missing: bool              # 文件不见了
    pages: int = 0             # PDF 页数（EPUB 为 0：EPUB 的页码按排版算，书架不知道）
    cover: str = ""            # 封面图路径，没有就是 ""
    quiz_done: list[int] = field(default_factory=list)
    ocr_done: bool = False     # 扫描版：本机文字识别已经做完（识别到一半是 False，OcrQueue 接着做）

    @property
    def file_name(self) -> str:
        return Path(self.path).name


def _entry_to_dict(e: BookEntry) -> dict:
    d = asdict(e)
    d.pop("missing", None)
    d["position"] = [e.position.doc, e.position.block] if e.position else None
    return d


def _entry_from_dict(d: dict) -> BookEntry:
    if not isinstance(d, dict):
        raise ValueError('书架条目格式不正确')
    for key in ('id', 'path', 'title'):
        if not isinstance(d.get(key), str) or not d[key] or '\x00' in d[key]:
            raise ValueError('书架条目缺少有效的标识、路径或书名')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', d['id']):
        raise ValueError('书架条目标识格式不正确')
    for key in ('author', 'kind', 'added', 'cover'):
        if key in d and not isinstance(d[key], str):
            raise ValueError('书架文字字段格式不正确')
    if d.get('kind') not in ('pdf', 'epub'):
        raise ValueError('书架文件格式不正确')
    for key in ('scanned', 'ocr_done'):
        if key in d and type(d[key]) is not bool:
            raise ValueError('书架识别状态格式不正确')
    if d.get('last_read') is not None and not isinstance(d['last_read'], str):
        raise ValueError('书架时间字段格式不正确')
    pos = d.get("position")
    if pos is not None and (not isinstance(pos, (list, tuple)) or len(pos) != 2
                            or any(type(n) is not int or n < 0 for n in pos)):
        raise ValueError('阅读位置格式不正确')
    if 'pages' in d and (type(d['pages']) is not int or d['pages'] < 0):
        raise ValueError('书页数量格式不正确')
    if 'quiz_done' in d and (not isinstance(d['quiz_done'], list)
                            or any(type(n) is not int or n < 0 for n in d['quiz_done'])):
        raise ValueError('小测记录格式不正确')
    known = {k: d[k] for k in BookEntry.__dataclass_fields__ if k in d and k not in ("position", "missing")}
    e = BookEntry(position=Loc(int(pos[0]), int(pos[1])) if pos else None, missing=False,
                  **{"author": "", "kind": "", "added": "", "last_read": None, "scanned": False,
                     **known})
    e.missing = not Path(e.path).is_file()
    return e


class Library:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.load_warnings: list[str] = []
        library_path = self.root / LIB_FILE
        loaded = _read(library_path)
        damaged = loaded is None and library_path.exists()
        self._data = loaded or {}
        for field, empty in (('books', {}), ('removed', {}), ('sessions', []), ('settings', {})):
            value = self._data.get(field, empty)
            if not isinstance(value, type(empty)):
                damaged = True
                value = empty
            self._data[field] = value
        for key in ('last_add_dir', 'auth_same_ok'):
            if key in self._data['settings'] and not isinstance(self._data['settings'][key], str):
                damaged = True
                self._data['settings'].pop(key)
        for field in ('books', 'removed'):
            valid = {}
            for bid, raw in self._data[field].items():
                try:
                    entry = _entry_from_dict(raw)
                    if entry.id != bid:
                        raise ValueError('书架标识不一致')
                    valid[bid] = raw
                except (ValueError, TypeError, KeyError, IndexError, OSError):
                    damaged = True
            self._data[field] = valid
        sessions = [r for r in self._data['sessions'] if isinstance(r, dict)
                    and isinstance(r.get('summary', {}), dict)
                    and isinstance(r.get('started', ''), str)
                    and isinstance(r.get('ended', ''), str)]
        damaged = damaged or len(sessions) != len(self._data['sessions'])
        self._data['sessions'] = sessions
        current = self._data.get('current')
        if current is not None and (not isinstance(current, str) or current not in self._data['books']):
            damaged = True
            self._data['current'] = None
        if damaged:
            backup = library_path.with_name(library_path.name + '.recovery-' +
                        datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
            try:
                shutil.copy2(library_path, backup)
            except OSError as e:
                raise ValueError('书架文件损坏且无法备份，请先备份数据目录后重试。') from e
            self.load_warnings.append('书架文件有损坏，原文件已备份为 ' + backup.name +
                                      '。可读取的书籍已保留；缺少的书可以重新添加。')
        self._data.setdefault("version", 1)
        self._data.setdefault("books", {})
        self._data.setdefault("removed", {})
        self._data.setdefault("current", None)
        self._data.setdefault("sessions", [])
        self._data.setdefault("settings", {})

    # ── 存盘 ─────────────────────────────────────────────
    @property
    def path(self) -> Path:
        return self.root / LIB_FILE

    def _save(self) -> None:
        _atomic_write(self.path, self._data)

    def _book_file(self, book_id: str) -> Path:
        return self.root / "books" / f"{book_id}.json"

    def _extra(self, book_id: str) -> dict:
        path = self._book_file(book_id)
        data = _read(path)
        if data is None and path.exists():
            backup = path.with_name(path.name + '.recovery-' +
                                   datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
            shutil.copy2(path, backup)
            self.load_warnings.append('阅读记录有损坏，原文件已备份为 ' + backup.name)
        return data or {}

    def _save_extra(self, book_id: str, data: dict) -> None:
        _atomic_write(self._book_file(book_id), data)

    # ── 书 ──────────────────────────────────────────────
    def books(self) -> list[BookEntry]:
        """按最近读过排；没读过的按加进来的先后（新的在前）排在后面。"""
        with self._lock:
            items = [_entry_from_dict(d) for d in self._data["books"].values()]
        return sorted(items, key=lambda e: (e.last_read or "", e.added or ""), reverse=True)

    def get(self, book_id: str) -> BookEntry | None:
        with self._lock:
            d = self._data["books"].get(book_id)
            return _entry_from_dict(d) if d else None

    def add(self, path: Path) -> BookEntry:
        """加一本书（要打开整本书，放在工作线程里调）。已经在书架上就原样返回。

        书架上还没有「在读」的书时，这本自动成为在读。打不开会抛 ValueError（带能给人看的原因）。
        """
        p = Path(path).expanduser().resolve()
        if p.suffix.lower() not in BOOK_EXTS:
            raise ValueError(f"只能放 PDF 或 EPUB，这个是 {p.suffix or '没有扩展名的文件'}")
        if not p.is_file():
            raise ValueError("找不到这个文件")
        bid = book_id_for(p)
        with self._lock:
            if bid in self._data["books"]:
                return self.get(bid)
            old = self._data["removed"].get(bid)

        from reader.books import open_book
        try:
            book = open_book(p)
        except Exception as e:  # noqa: BLE001
            raise ValueError("这本书无法打开，请检查文件是否完整、是否需要密码，或重新下载后再添加。") from e
        title, guess_author = clean_title(book.title, p.stem)
        kind = book.kind
        extra = {"sections": [[s.title, s.start.doc, s.start.block, s.level] for s in book.sections],
                 "toc": [[t.title, t.loc.doc, t.loc.block, t.level] for t in book.toc[:2000]],
                 "highlights": []}
        from reader.books.ocr import file_key
        extra['source_key'] = file_key(p)
        pages, scanned, ocr_done = 0, False, False
        if kind == "pdf":
            pages = book.pages
            scanned = not book.has_text
            store = getattr(book, "ocr", None)       # 同一个文件以前识别过（按内容认）：直接算做完
            ocr_done = bool(scanned and store is not None and store.done_pages(pages) >= pages)
        else:
            extra["doc_sizes"] = [len(b) for b in book.blocks]
        cover = self._make_cover(bid, book)
        try:
            if kind == "pdf":
                book.doc.close()
            else:
                book.zip.close()
        except Exception:  # noqa: BLE001
            pass
        entry = BookEntry(id=bid, path=str(p), title=title, author=book.author or guess_author,
                          kind=kind, position=None, added=_now(), last_read=None,
                          scanned=scanned, missing=False, pages=pages, cover=cover, ocr_done=ocr_done)
        with self._lock:
            if old is not None:
                # Validate and refresh the file before restoring any reading records.
                previous_extra = self._extra(bid)
                same_file = previous_extra.get('source_key', extra['source_key']) == extra['source_key']
                if same_file:
                    entry.position = _entry_from_dict(old).position
                    entry.last_read = old.get('last_read')
                    entry.quiz_done = old.get('quiz_done', [])
                    extra['highlights'] = previous_extra.get('highlights', [])
                if entry.position is not None and kind == 'pdf':
                    entry.position = Loc(min(entry.position.doc, max(0, pages - 1)), 0)
                self._data['removed'].pop(bid, None)
            self._save_extra(bid, extra)
            self._data["books"][bid] = _entry_to_dict(entry)
            if not self._data["current"]:
                self._data["current"] = bid
            self._save()
        return entry

    def _make_cover(self, bid: str, book) -> str:
        """封面存成图片；取不到就空着（书架画一个素色的）。"""
        out = self.root / "covers"
        try:
            out.mkdir(parents=True, exist_ok=True)
            if book.kind == "pdf":
                if not book.pages:
                    return ""
                import pymupdf
                page = book.doc[0]
                zoom = 300 / max(1.0, page.rect.width)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                dst = out / f"{bid}.png"
                pix.save(str(dst))
                return str(dst)
            data, ext = epub_cover(book.zip)
            if data:
                dst = out / f"{bid}{ext}"
                dst.write_bytes(data)
                return str(dst)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def remove(self, book_id: str) -> None:
        """从书架拿掉（不动原文件）。再加同一个文件时原样回来。"""
        with self._lock:
            d = self._data["books"].pop(book_id, None)
            if d is None:
                return
            self._data["removed"][book_id] = d
            if self._data["current"] == book_id:
                rest = self.books()
                self._data["current"] = rest[0].id if rest else None
            self._save()

    def relink(self, book_id: str, new_path: Path) -> BookEntry:
        """文件挪了地方：指到新位置（id 不变，读到哪都还在）。"""
        p = Path(new_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError("找不到这个文件")
        entry = self.get(book_id)
        if entry is None:
            raise ValueError('这本书已经从书架拿掉了')
        if p.suffix.lower() != '.' + entry.kind:
            raise ValueError('请选择与原书相同格式的 PDF 或 EPUB')
        with self._lock:
            if any(b.id != book_id and book_id_for(b.path) == book_id_for(p)
                   for b in self.books()):
                raise ValueError('这个文件已经在书架上，请选择原来那本书的文件。')
        from reader.books import open_book
        book = None
        try:
            book = open_book(p)
            if book.kind != entry.kind:
                raise ValueError('请选择与原书相同格式的文件')
            title, guessed_author = clean_title(book.title, p.stem)
            metadata = {'title': title, 'author': book.author or guessed_author,
                        'pages': book.pages if book.kind == 'pdf' else 0,
                        'scanned': book.kind == 'pdf' and not book.has_text,
                        'cover': self._make_cover(book_id, book)}
            extra = self._extra(book_id)
            extra.update(sections=[[s.title, s.start.doc, s.start.block, s.level] for s in book.sections],
                         toc=[[t.title, t.loc.doc, t.loc.block, t.level] for t in book.toc[:2000]])
            if book.kind == 'epub':
                extra['doc_sizes'] = [len(b) for b in book.blocks]
        except Exception as e:
            raise ValueError('这个文件无法作为原书打开，请重新选择。') from e
        finally:
            if book is not None:
                (book.doc if book.kind == 'pdf' else book.zip).close()
        with self._lock:
            d = self._data["books"][book_id]
            d["path"] = str(p)
            d.update(metadata)
            d['ocr_done'] = False
            if entry.kind == 'pdf' and d.get('position'):
                d['position'] = [min(d['position'][0], max(0, metadata['pages'] - 1)), 0]
            self._save_extra(book_id, extra)
            self._save()
        return self.get(book_id)

    def current(self) -> BookEntry | None:
        with self._lock:
            bid = self._data["current"]
        return self.get(bid) if bid else None

    def set_current(self, book_id: str) -> None:
        with self._lock:
            if book_id not in self._data["books"]:
                raise KeyError(book_id)
            self._data["current"] = book_id
            self._save()

    # ── 读到哪 ───────────────────────────────────────────
    def save_position(self, book_id: str, loc: Loc) -> None:
        with self._lock:
            d = self._data["books"].get(book_id)
            if d is None:
                return
            d["position"] = [int(loc.doc), int(loc.block)]
            d["last_read"] = _now()
            self._save()

    def progress(self, book_id: str) -> float | None:
        """读到百分之几（0～1）；没开始读是 None。"""
        e = self.get(book_id)
        if e is None or e.position is None:
            return None
        if e.kind == "pdf":
            return min(e.pages, max(0, e.position.doc + 1)) / e.pages if e.pages else None
        sizes = self._extra(book_id).get("doc_sizes") or []
        if not isinstance(sizes, list) or any(type(n) is not int or n < 0 for n in sizes):
            return None
        total = sum(sizes)
        if not total:
            return None
        done = sum(sizes[:e.position.doc]) + e.position.block
        return min(total, max(0, done)) / total

    def place(self, book_id: str, loc: Loc | None = None) -> dict:
        """某个位置在书里的哪儿：{"section": 最细的小节名, "chapter": 最上一层的章名}（没有就是 ""）。"""
        e = self.get(book_id)
        loc = loc or (e.position if e else None)
        out = {"section": "", "chapter": ""}
        if loc is None:
            return out
        extra = self._extra(book_id)
        toc = extra.get("toc") or []
        best = None
        top = None
        for title, d, b, level in toc:
            if Loc(d, b) <= loc:
                if best is None or Loc(d, b) >= Loc(best[1], best[2]):
                    best = (title, d, b, level)
                if level == 1 and (top is None or Loc(d, b) >= Loc(top[1], top[2])):
                    top = (title, d, b)
        if best is None:
            for title, d, b, _lv in extra.get("sections") or []:
                if Loc(d, b) <= loc:
                    best = (title, d, b, 1)
        if best:
            out["section"] = best[0]
        if top and (not best or top[0] != best[0]):
            out["chapter"] = top[0]
        return out

    # ── 高亮 ─────────────────────────────────────────────
    def highlights(self, book_id: str) -> list[dict]:
        with self._lock:
            return list(self._extra(book_id).get("highlights") or [])

    def save_highlights(self, book_id: str, items: list[dict]) -> None:
        with self._lock:
            extra = self._extra(book_id)
            extra["highlights"] = list(items)
            self._save_extra(book_id, extra)

    # ── 小测 ─────────────────────────────────────────────
    def mark_ocr_done(self, book_id: str, done: bool = True) -> None:
        with self._lock:
            d = self._data["books"].get(book_id)
            if d is None:
                return
            d["ocr_done"] = bool(done)
            self._save()

    def quiz_done(self, book_id: str) -> set[int]:
        e = self.get(book_id)
        return set(e.quiz_done) if e else set()

    def refresh_structure(self, book_id: str, book) -> None:
        """开书时把书架存的目录、小节换成这次拆出来的（拆书的规则改过，比如目录纠错）。
        小节变了，按小节号记的「答过的题」就对不上了：清掉（那几节会再出一次题）。"""
        secs = [[s.title, s.start.doc, s.start.block, s.level] for s in book.sections]
        toc = [[t.title, t.loc.doc, t.loc.block, t.level] for t in book.toc[:2000]]
        with self._lock:
            d = self._data["books"].get(book_id)
            if d is None:
                return
            extra = self._extra(book_id)
            if extra.get("sections") == secs and extra.get("toc") == toc:
                return
            old = extra.get("sections")
            extra["sections"], extra["toc"] = secs, toc
            self._save_extra(book_id, extra)
            # 只管 EPUB：扫描版 PDF 的小节会随识别进度变（按字数并节），但扫描版本来就不出题
            if book.kind == "epub" and old is not None and old != secs and d.get("quiz_done"):
                d["quiz_done"] = []
                self._save()

    def mark_quiz_done(self, book_id: str, section: int) -> None:
        with self._lock:
            d = self._data["books"].get(book_id)
            if d is None:
                return
            done = set(d.get("quiz_done") or [])
            done.add(int(section))
            d["quiz_done"] = sorted(done)
            self._save()

    # ── 读书笔记 ─────────────────────────────────────────
    @property
    def notes_root(self) -> Path:
        return self.root / NOTES_DIR

    def _notes_dir(self, book_id: str) -> Path:
        e = self.get(book_id)
        with self._lock:
            d = self._data["books"].get(book_id) or self._data["removed"].get(book_id) or {}
        title = e.title if e else d.get("title", book_id)
        return self.notes_root / safe_name(title)

    def save_note(self, book_id: str, markdown: str, when: datetime) -> Path:
        """存成 笔记\\<书名>\\<日期> 第 N 次.md（N = 这本书这一天的第几次），返回路径。"""
        folder = self._notes_dir(book_id)
        folder.mkdir(parents=True, exist_ok=True)
        day = when.strftime("%Y-%m-%d")
        with self._lock:
            n = 1
            while (folder / f"{day} 第 {n} 次.md").exists():
                n += 1
            dst = folder / f"{day} 第 {n} 次.md"
            tmp = dst.with_suffix(".md.tmp")
            tmp.write_text(markdown, encoding="utf-8")
            os.replace(tmp, dst)
            ts = when.timestamp()
            os.utime(dst, (ts, ts))
        return dst

    def notes(self, book_id: str | None = None) -> list[Path]:
        """读书笔记文件，新的在前。"""
        base = self._notes_dir(book_id) if book_id else self.notes_root
        pattern = "*.md" if book_id else "*/*.md"
        try:
            files = [p for p in base.glob(pattern) if p.is_file()]
        except OSError:
            return []
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    # ── 每次读书的记录（书架「今天读了 N 次（08:40–08:58）」、笔记页的总结） ─────
    def log_session(self, book_id: str, summary: dict, started: datetime, ended: datetime,
                    **extra) -> dict:
        """记一次读书。summary 是 ReadingScreen.finished 发的总结；extra 比如 section=读到的小节名。"""
        rec = {"book_id": book_id, "started": started.isoformat(timespec="seconds"),
               "ended": ended.isoformat(timespec="seconds"),
               "summary": {k: _jsonable(v) for k, v in (summary or {}).items() if not callable(v)},
               **{k: _jsonable(v) for k, v in extra.items()}}
        with self._lock:
            self._data["sessions"].append(rec)
            self._data["sessions"] = self._data["sessions"][-500:]
            self._save()
        return rec

    def update_session(self, rec: dict, **summary_fields) -> None:
        """改一次读书记录的总结（比如读书笔记后来才写好：note_path）。rec 是 log_session 返回的那条。"""
        with self._lock:
            for s in reversed(self._data["sessions"]):
                if s.get("book_id") == rec.get("book_id") and s.get("started") == rec.get("started"):
                    s.setdefault("summary", {}).update(
                        {k: _jsonable(v) for k, v in summary_fields.items() if not callable(v)})
                    self._save()
                    return

    def sessions(self, day: str | None = None) -> list[dict]:
        """某天（YYYY-MM-DD）的读书记录，按开始时间排；day=None 是全部。"""
        with self._lock:
            items = list(self._data["sessions"])
        if day:
            items = [s for s in items if s.get("started", "").startswith(day)]
        return sorted(items, key=lambda s: s.get("started", ""))

    def session_for_note(self, note: Path) -> dict | None:
        key = os.path.normcase(str(note))
        with self._lock:
            for s in reversed(self._data["sessions"]):
                p = (s.get("summary") or {}).get("note_path")
                if p and os.path.normcase(str(p)) == key:
                    return s
        return None

    # ── 小设置（上次加书的文件夹、同账号确认过了……） ──────────
    def setting(self, key: str, default=None):
        with self._lock:
            return self._data["settings"].get(key, default)

    def set_setting(self, key: str, value) -> None:
        with self._lock:
            self._data["settings"][key] = value
            self._save()


class OcrQueue:
    """扫描版 PDF 的本机文字识别（reader/books/ocr.py），一次只做一本，在后台线程里做。

    - 加书时发现是扫描版就 enqueue；启动时 resume_all() 把没做完的接着做（识别结果按页存，断了接着来）。
    - 界面每秒问一次 state(book_id) 更新那一行的字：("running", 已识别页, 总页) / ("queued", …) /
      ("done", …) / ("unavailable", …) / None（不是扫描版）。
    - stop()：退出时调，当前这页识别完就停，已识别的都存下了。
    runner 给测试换掉（默认 reader.books.ocr.run_book）。
    """

    def __init__(self, library: "Library", *, runner=None, available: bool | None = None,
                 counter=None):
        self.library = library
        self._runner = runner
        self._counter = counter            # (pdf 路径) -> 已识别页数；给测试换掉
        self._available = available
        self._lock = threading.Lock()
        self._todo: list[str] = []
        self._progress: dict[str, tuple[int, int]] = {}
        self._current: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: dict[str, str] = {}

    def available(self) -> bool:
        if self._available is None:
            try:
                from reader.books import ocr
                self._available = ocr.available()
            except Exception:  # noqa: BLE001
                self._available = False
        return self._available

    def state(self, book_id: str) -> tuple[str, int, int] | None:
        e = self.library.get(book_id)
        if e is None or not e.scanned:
            return None
        if e.ocr_done:
            return "done", e.pages, e.pages
        if not self.available():
            return "unavailable", 0, e.pages
        with self._lock:
            done, total = self._progress.get(book_id, (0, e.pages))
            if self._current == book_id:
                return "running", done, total
            if book_id in self._todo:
                return "queued", done, total
        return "paused", done, e.pages

    def busy(self) -> bool:
        with self._lock:
            return self._current is not None or bool(self._todo)

    def running_path(self) -> str | None:
        """正在识别的那本书的文件路径；没在识别返回 None。
        （书页那边不拿这个对象，用 reader.books.ocr.is_running / OcrStore.running 看同一件事。）"""
        with self._lock:
            bid = self._current
        e = self.library.get(bid) if bid else None
        return e.path if e is not None else None

    def enqueue(self, entry: BookEntry) -> None:
        if not entry.scanned or entry.ocr_done or entry.missing or not self.available():
            return
        with self._lock:
            if entry.id == self._current or entry.id in self._todo:
                return
            self._todo.append(entry.id)
            self._progress.setdefault(entry.id, (0, entry.pages))
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._work, name="reader-ocr", daemon=True)
                self._thread.start()

    def resume_all(self) -> None:
        for e in self.library.books():
            self.enqueue(e)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            self._todo.clear()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if not self._todo:
                    self._current = None
                    self._thread = None
                    return
                bid = self._current = self._todo.pop(0)
            e = self.library.get(bid)
            if e is None or e.missing:
                continue
            try:
                self._run_one(e)
            except Exception as ex:  # noqa: BLE001
                self.errors[bid] = f"{type(ex).__name__}: {ex}"
        with self._lock:
            self._current = None

    def _run_one(self, e: BookEntry) -> None:
        from reader.books import ocr
        path = Path(e.path)
        before = self._counter(path) if self._counter else ocr.OcrStore(path).done_pages()
        total = e.pages or 1
        with self._lock:
            self._progress[e.id] = (before, total)

        def cb(i: int, _n: int) -> None:
            with self._lock:
                self._progress[e.id] = (min(total, before + i), total)

        runner = self._runner or ocr.run_book
        store = runner(path, title=e.title, progress=cb, stop=self._stop)
        done = (store.done_pages(e.pages) if isinstance(store, ocr.OcrStore) else store.done_pages()) \
            if store is not None else self._progress[e.id][0]
        with self._lock:
            self._progress[e.id] = (done, total)
        if done >= total:
            self.library.mark_ocr_done(e.id)


def _jsonable(v):
    if isinstance(v, Loc):
        return [v.doc, v.block]
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if hasattr(v, "__dataclass_fields__"):
        return _jsonable(asdict(v))
    return str(v)


def epub_cover(z: zipfile.ZipFile) -> tuple[bytes, str]:
    """EPUB 里的封面图：(图片字节, 扩展名)。找不到返回 (b"", "")。"""
    names = set(z.namelist())
    opf = ""
    if "META-INF/container.xml" in names:
        m = re.search(r'full-path\s*=\s*"([^"]+)"', z.read("META-INF/container.xml").decode("utf-8", "ignore"))
        opf = m.group(1) if m else ""
    if opf not in names:
        opf = next((n for n in names if n.lower().endswith(".opf")), "")
    if not opf:
        return b"", ""
    s = z.read(opf).decode("utf-8", "ignore")
    base = posixpath.dirname(opf)
    items = re.findall(r"<item\b[^>]*>", s)

    def attr(tag, name):
        m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag) or re.search(rf"\b{name}\s*=\s*'([^']*)'", tag)
        return m.group(1) if m else ""

    href = ""
    for it in items:                                  # EPUB 3：properties="cover-image"
        if "cover-image" in attr(it, "properties"):
            href = attr(it, "href")
            break
    if not href:                                      # EPUB 2：<meta name="cover" content="id">
        m = re.search(r'<meta\b[^>]*name\s*=\s*"cover"[^>]*>', s)
        cid = attr(m.group(0), "content") if m else ""
        for it in items:
            if cid and attr(it, "id") == cid:
                href = attr(it, "href")
                break
    if not href:                                      # 最后：叫 cover 的图片
        for it in items:
            h = attr(it, "href")
            if "cover" in h.lower() and attr(it, "media-type").startswith("image/"):
                href = h
                break
    if not href:
        return b"", ""
    from urllib.parse import unquote
    path = posixpath.normpath(posixpath.join(base, unquote(href)))
    if path not in names:
        return b"", ""
    ext = posixpath.splitext(path)[1].lower() or ".jpg"
    return z.read(path), ext


if __name__ == "__main__":
    # 演示：在临时文件夹里建一个书架，把 ~/Downloads 里的两本真书放进去，打印出来。不碰真的书架。
    import sys
    import time

    tmp = Path(tempfile.mkdtemp(prefix="reader-lib-"))
    lib = Library(tmp)
    dl = Path.home() / "Downloads"
    picks = [next(iter(dl.glob("*.epub")), None), next(iter(dl.glob("*.pdf")), None)]
    for p in picks:
        if p is None:
            continue
        t0 = time.time()
        e = lib.add(p)
        print(f"加了《{e.title}》 {e.kind} 作者={e.author!r} 扫描版={e.scanned} "
              f"页数={e.pages} 封面={'有' if e.cover else '没有'} 用了 {time.time() - t0:.1f} 秒")
    for e in lib.books():
        lib.save_position(e.id, Loc(3, 5))
        print(" ", e.title, "进度", f"{(lib.progress(e.id) or 0):.0%}", lib.place(e.id))
    cur = lib.current()
    print("在读：", cur.title if cur else None)
    if cur:
        print("笔记存到：", lib.save_note(cur.id, "# 演示\n\n- 一条", datetime.now()))
    print("书架文件：", lib.path)
    sys.exit(0)
