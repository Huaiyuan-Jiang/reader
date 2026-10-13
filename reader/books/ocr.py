"""扫描版 PDF 的文字识别：用 Windows 自带的 OCR（Windows.Media.Ocr）。

2026-09-24 定：扫描版「两个都要」—— 平时用本机识别出来的文字（AI 读得到、能出题、能选字划线），
问到图表时再把那一页的图片发给 Claude（见 ai/companion.py 的 wants_page_image）。

- 不联网、不花额度。用系统装好的识别语言（中文要 zh-Hans-CN，英文要 en-US）。
- 实测（2026-09-24，一本扫描书）：一页 0.2 秒，中英文都准。300 页的书一分钟左右。
- 依赖：winrt-runtime、winrt-Windows.Media.Ocr / Graphics.Imaging / Storage.Streams / Foundation(.Collections) / Globalization
  （都是几百 KB 的小包）。没装就当识别不了，照样能读书。
- 中文结果字和字之间带空格（Windows OCR 按「词」出，中文一个字一个词），合并时去掉 CJK 之间的空格。
- 结果按页存在 `%LOCALAPPDATA%\\reader\\ocr\\<文件哈希>.json`，识别到一半关掉下次接着来。
  坐标换算成 PDF 坐标（跟 page.get_text("words") 一样），选字 / 划线 / 找出处都能直接用。
- 读书时书页手里的 OcrStore 跟后台识别（OcrQueue → run_book）用的不是同一个对象：
  书页那边 page() / done_pages() 发现文件更新了就重读（每次最多看一眼文件的修改时间）。
  「现在是不是真的在识别这本」看 running()：run_book 跑着的时候登记在本进程里，跑完就撤。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Callable

SCALE = 2.0            # 识别用的出图倍数（2 倍在实测的扫描书上够准，也快）
_CJK = r"　-〿㐀-鿿＀-￯"
_CJK_GAP = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


# 正在识别的书（结果文件路径 → 有几个 run_book 在跑）。后台识别和书页在同一个进程里，登记在这里就够了
_running: dict[str, int] = {}
_running_lock = threading.Lock()


def _mark_running(key: str, on: bool) -> None:
    with _running_lock:
        n = _running.get(key, 0) + (1 if on else -1)
        if n > 0:
            _running[key] = n
        else:
            _running.pop(key, None)


def is_running(pdf_path: Path) -> bool:
    """这本书现在是不是真的有识别在跑（本进程里）。"""
    try:
        key = str(ocr_dir() / f"{file_key(pdf_path)}.json")
    except OSError:
        return False
    with _running_lock:
        return key in _running


def available() -> bool:
    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def languages() -> list[str]:
    try:
        from winrt.windows.media.ocr import OcrEngine
        return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    except Exception:  # noqa: BLE001
        return []


def ocr_dir() -> Path:
    d = Path(os.environ.get("READER_DATA_DIR") or
             Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "reader") / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_key(path: Path) -> str:
    """按文件内容认（改名、挪位置都还认得）。只读头尾各 1MB + 大小，大文件也快。"""
    p = Path(path)
    h = hashlib.sha1()
    size = p.stat().st_size
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:20]


def join_cjk(text: str) -> str:
    return _CJK_GAP.sub("", text)


async def _recognize(png: bytes, lang: str):
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    stream = InMemoryRandomAccessStream()
    w = DataWriter(stream)
    w.write_bytes(png)
    await w.store_async()
    await w.flush_async()
    w.detach_stream()
    stream.seek(0)
    dec = await BitmapDecoder.create_async(stream)
    bmp = await dec.get_software_bitmap_async()
    eng = OcrEngine.try_create_from_language(Language(lang))
    if eng is None:
        raise RuntimeError(f"系统没有 {lang} 的文字识别")
    return await eng.recognize_async(bmp)


def ocr_page(page, lang: str) -> dict:
    """识别一页（pymupdf.Page）。返回 {"text": 整页文字, "words": [[x0,y0,x1,y1,词,行号], ...]}（PDF 坐标）。"""
    import pymupdf
    png = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE)).tobytes("png")
    res = asyncio.run(_recognize(png, lang))
    lines, words = [], []
    for li, line in enumerate(res.lines):
        lines.append(join_cjk(line.text))
        for w in line.words:
            r = w.bounding_rect
            words.append([r.x / SCALE, r.y / SCALE, (r.x + r.width) / SCALE,
                          (r.y + r.height) / SCALE, w.text, li])
    return {"text": "\n".join(lines), "words": words}


def guess_lang(sample: str, title: str = "") -> str:
    """中文字多就用中文识别，否则英文。拿书名和已识别的一点样本判断。"""
    text = f"{title}\n{sample}"
    cjk = len(re.findall(r"[一-鿿]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return "zh-Hans-CN" if cjk >= max(4, latin // 4) else "en-US"


class OcrStore:
    """一本书的识别结果（按页），存成 JSON。线程安全：后台识别在写，界面在读。"""

    def __init__(self, pdf_path: Path):
        self.path = ocr_dir() / f"{file_key(pdf_path)}.json"
        self._lock = threading.Lock()
        self.data = {"lang": "", "pages": {}}
        self._mtime = 0.0
        self._reload()

    def _reload(self) -> None:
        """从文件重读（后台识别是另一个 OcrStore 在写：读书途中新识别的页要能用上）。"""
        try:
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            # 自己手里有、文件里还没有的页（本对象刚识别、还没存）留着
            pages = {**data.get("pages", {}), **{k: v for k, v in self.data["pages"].items()
                                                 if k not in data.get("pages", {})}}
            self.data = {**data, "pages": pages}
            self._mtime = mtime

    @property
    def lang(self) -> str:
        return self.data.get("lang", "")

    def page(self, p: int) -> dict | None:
        with self._lock:
            hit = self.data["pages"].get(str(p))
        if hit is None:
            self._reload()               # 可能后台刚识别到这页
            with self._lock:
                hit = self.data["pages"].get(str(p))
        return hit

    def done_pages(self) -> int:
        """已识别的页数。后台可能刚存了新的页：先看一眼文件（一次 stat，没变就不读）。"""
        self._reload()
        with self._lock:
            return len(self.data["pages"])

    def running(self) -> bool:
        """现在有没有 run_book 在识别这本书（登记见 _running）。"""
        with _running_lock:
            return str(self.path) in _running

    def put(self, p: int, result: dict, lang: str) -> None:
        with self._lock:
            self.data["lang"] = lang
            self.data["pages"][str(p)] = result

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)


def run_book(pdf_path: Path, *, title: str = "", pages: list[int] | None = None,
             progress: Callable[[int, int], None] | None = None,
             stop: threading.Event | None = None, save_every: int = 10) -> OcrStore:
    """把一本书（或指定的页）识别完，跳过已经识别过的页。在工作线程里调。

    语言：先用中文识别前 3 页没识别过的页，看结果定中文还是英文（定了就存下，以后不再猜）。
    """
    store = OcrStore(pdf_path)
    key = str(store.path)
    _mark_running(key, True)
    try:
        return _run(store, pdf_path, title=title, pages=pages, progress=progress, stop=stop,
                    save_every=save_every)
    finally:
        _mark_running(key, False)


def _run(store: OcrStore, pdf_path: Path, *, title: str, pages: list[int] | None,
         progress: Callable[[int, int], None] | None, stop: threading.Event | None,
         save_every: int) -> OcrStore:
    import pymupdf
    doc = pymupdf.open(pdf_path)
    todo = [p for p in (pages if pages is not None else range(doc.page_count))
            if store.page(p) is None]
    lang = store.lang
    if not lang:
        sample = ""
        for p in range(min(doc.page_count, 12)):
            if len(sample) > 200:
                break
            try:
                sample += ocr_page(doc[p], "zh-Hans-CN")["text"]
            except Exception:  # noqa: BLE001
                break
        lang = guess_lang(sample, title)
        if lang not in languages():
            lang = "zh-Hans-CN" if "zh-Hans-CN" in languages() else (languages() or ["en-US"])[0]
    total = len(todo)
    for i, p in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        try:
            store.put(p, ocr_page(doc[p], lang), lang)
        except Exception as e:  # noqa: BLE001
            store.put(p, {"text": "", "words": [], "error": f"{type(e).__name__}: {e}"}, lang)
        if progress:
            progress(i + 1, total)
        if (i + 1) % save_every == 0:
            store.save()
    store.save()
    return store
