"""书的拆分 + 读书伙伴（假 AI，不花额度）。运行：python -m unittest discover -s tests"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import pymupdf

from reader.ai.claude import Delta, Result
from reader.ai.companion import Companion, extract_json, locate_quote
from reader.books import Loc, open_book
from reader.books.model import is_footnote_title

LONG = "这是一段用来凑长度的正文，讲的是惩罚方式怎样从公开的酷刑变成安静的监禁。" * 12   # 约 450 字


def make_epub(path: Path, chapters: list[tuple[str, list[str]]], toc: bool = True,
              extra_toc: list[str] = (), toc_titles: dict[int, str] | None = None,
              toc_srcs: dict[int, str] | None = None) -> Path:
    """toc_titles / toc_srcs：第 i 项目录的标题 / 指向（默认是第 i 章的标题、ch{i}.xhtml#c{i}）。"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items, refs, nav = [], [], []
        for i, (title, paras) in enumerate(chapters):
            body = f'<h1 id="c{i}">{title}</h1>' + "".join(f"<p>{p}</p>" for p in paras)
            z.writestr(f"OEBPS/ch{i}.xhtml", f"<html><head><style>p{{}}</style></head><body>{body}</body></html>")
            items.append(f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            refs.append(f'<itemref idref="ch{i}"/>')
            nav.append(f'<navPoint id="n{i}"><navLabel><text>{(toc_titles or {}).get(i, title)}</text></navLabel>'
                       f'<content src="{(toc_srcs or {}).get(i, f"ch{i}.xhtml#c{i}")}"/></navPoint>')
        for j, t in enumerate(extra_toc):
            nav.append(f'<navPoint id="x{j}"><navLabel><text>{t}</text></navLabel>'
                       f'<content src="ch0.xhtml"/></navPoint>')
        if toc:
            items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
            z.writestr("OEBPS/toc.ncx", f"<ncx><navMap>{''.join(nav)}</navMap></ncx>")
        z.writestr("OEBPS/content.opf",
                   '<package><metadata><dc:title>测试之书</dc:title><dc:creator>某作者</dc:creator></metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine>{"".join(refs)}</spine></package>')
    return path


def make_pdf(path: Path, pages: int, toc: list | None = None) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 72), f"Page {i + 1}. " + "The quick brown fox jumps over the lazy dog. " * 3)
        pg.insert_text((72, 100), "Punishment moved from public spectacle to quiet discipline." * 2)
    if toc:
        doc.set_toc(toc)
    doc.save(path)
    return path


class FakeClaude:
    """记下每次调用；按顺序吐出预设回答。"""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def run(self, prompt, *, system=None, resume=None, stop=None, **kw):
        self.calls.append({"prompt": prompt, "system": system, "resume": resume})
        text = self.answers.pop(0) if self.answers else "好的"
        yield Delta(text[:3])
        yield Delta(text[3:])
        yield Result(text=text, session_id="sess-1")

    def ask_once(self, prompt, *, system, timeout=240, stop=None):
        self.calls.append({"prompt": prompt, "system": system, "resume": None})
        return Result(text=self.answers.pop(0) if self.answers else "{}")


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_toc_sections_merge_and_split(self):
        chapters = [("扉页", ["短"]),                           # 太短 → 并进下一节
                    ("第一章 酷刑", [LONG] * 6),                  # 约 2700 字 → 一节
                    ("第二章 惩罚的温和方式", [LONG] * 40)]       # 约 18000 字 → 切开
        b = open_book(make_epub(self.tmp / "a.epub", chapters))
        self.assertEqual(b.title, "测试之书")
        self.assertEqual(b.author, "某作者")
        titles = [s.title for s in b.sections]
        self.assertEqual(titles[0], "第一章 酷刑")               # 扉页并进来，标题用第一章的
        self.assertEqual(b.sections[0].start, Loc(0, 0))
        self.assertTrue(all(t.startswith("第二章") for t in titles[1:]))
        self.assertGreaterEqual(len(titles), 3)
        # 首尾相接、覆盖全书
        for a, c in zip(b.sections, b.sections[1:]):
            self.assertEqual(a.end, c.start)
        self.assertEqual(b.sections[-1].end, b.end())
        self.assertIn("惩罚方式", b.sections[1].text())

    def test_no_toc_uses_headings(self):
        b = open_book(make_epub(self.tmp / "b.epub",
                                [("甲", [LONG] * 5), ("乙", [LONG] * 5)], toc=False))
        self.assertEqual([s.title for s in b.sections], ["甲", "乙"])

    def test_footnote_toc_entries_dropped(self):
        b = open_book(make_epub(self.tmp / "c.epub", [("第一章", [LONG] * 5)],
                                extra_toc=["[1]", "(2)", "【3】"]))
        self.assertEqual([e.title for e in b.toc], ["第一章"])
        for t in ["[12]", "(3)", "（4）", "12", "〔7〕"]:
            self.assertTrue(is_footnote_title(t), t)
        for t in ["第一版序言[1]", "1984", "Chapter 1"]:
            self.assertFalse(is_footnote_title(t) and not t.isdigit(), t)

    def test_wrong_toc_repaired_by_headings(self):
        # 转制的书：目录指错了文件，章名里还带注释编号
        chapters = [("版权信息", ["短"]), ("序", [LONG] * 5), ("第一章 导论 [1]", [LONG] * 5),
                    ("第二章 文化根源", [LONG] * 5), ("译后记", [LONG] * 2)]
        b = open_book(make_epub(self.tmp / "w.epub", chapters,
                                toc_titles={0: "版权页", 2: "第一章 导论"},
                                toc_srcs={2: "ch1.xhtml#p3", 3: "ch1.xhtml", 4: "ch3.xhtml"}))
        self.assertEqual([(e.title, e.loc) for e in b.toc],
                         [("版权页", Loc(0, 0)), ("序", Loc(1, 0)), ("第一章 导论", Loc(2, 0)),
                          ("第二章 文化根源", Loc(3, 0)), ("译后记", Loc(4, 0))])
        self.assertEqual(b.place_title(Loc(2, 3)), "第一章 导论")
        self.assertEqual(b.place_title(Loc(4, 1)), "译后记")
        self.assertEqual(b.section_at(Loc(2, 1)).title, "第一章 导论")      # 小节（小测卡的标题）也跟着对了
        self.assertEqual(b.section_at(Loc(2, 1)).start, Loc(2, 0))

    def test_locate_quote(self):
        paras = [LONG, "在人们看来，这种惩罚方式，其野蛮程度不亚于，甚至超过犯罪本身。", LONG]
        b = open_book(make_epub(self.tmp / "d.epub", [("第一章", paras * 2)]))
        sec = b.sections[0]
        self.assertEqual(locate_quote(b, sec, "其野蛮程度不亚于，甚至超过犯罪本身"), Loc(0, 2))
        # 标点、空白不同也能找到
        self.assertEqual(locate_quote(b, sec, "其野蛮程度不亚于 甚至超过犯罪本身。"), Loc(0, 2))
        # 有点出入（AI 改了一两个字）：按相似度
        self.assertEqual(locate_quote(b, sec, "这种惩罚方式其野蛮程度不亚于甚至超过了犯罪本身"), Loc(0, 2))
        self.assertIsNone(locate_quote(b, sec, "完全不相干的一句话，书里根本没有"))
        self.assertIsNone(locate_quote(b, sec, "短"))


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_no_toc_every_ten_pages(self):
        b = open_book(make_pdf(self.tmp / "a.pdf", 25))
        self.assertTrue(b.has_text)
        self.assertEqual([(s.start.doc, s.end.doc) for s in b.sections], [(0, 10), (10, 20), (20, 25)])
        self.assertIn("〔第 11 页〕", b.sections[1].text())

    def test_toc_and_long_section_split(self):
        b = open_book(make_pdf(self.tmp / "b.pdf", 40, toc=[[1, "Intro", 1], [1, "Part One", 5]]))
        titles = [s.title for s in b.sections]
        # Intro 只有 4 页、字少 → 并进 Part One（起点仍是第 1 页）；Part One 36 页 → 按 10 页切
        self.assertTrue(all(t.startswith("Part One（") for t in titles), titles)
        self.assertEqual(b.sections[0].start, Loc(0))
        self.assertEqual(len(titles), 4)
        self.assertEqual(b.sections[-1].end, b.end())

    def test_locate_quote_page(self):
        b = open_book(make_pdf(self.tmp / "c.pdf", 12))
        self.assertEqual(locate_quote(b, b.sections[0], "Page 4. The quick brown fox"), Loc(3))


class CompanionTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.book = open_book(make_epub(tmp / "a.epub", [
            ("第一章 酷刑", [LONG] * 5 + ["在人们看来，这种惩罚方式，其野蛮程度不亚于，甚至超过犯罪本身。"]),
            ("第二章 监狱", [LONG] * 5)]))

    def test_ask_sends_section_once_and_resumes(self):
        fake = FakeClaude(["第一答", "第二答", "第三答"])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        evs = list(c.ask("这章讲什么？", Loc(0, 1), selection="选中的字"))
        self.assertEqual("".join(e.text for e in evs if isinstance(e, Delta)), "第一答")
        first = fake.calls[0]
        self.assertIn("测试之书", first["system"])
        self.assertIn("【这一节的原文：第一章 酷刑】", first["prompt"])
        self.assertIn("「选中的字」", first["prompt"])
        self.assertIsNone(first["resume"])
        list(c.ask("再说说？", Loc(0, 2)))
        second = fake.calls[1]
        self.assertIsNone(second["system"])                 # 续会话不再给系统提示
        self.assertEqual(second["resume"], "sess-1")
        self.assertNotIn("这一节的原文", second["prompt"])     # 同一节原文不重发
        list(c.ask("下一章呢？", Loc(1, 1)))
        self.assertIn("【这一节的原文：第二章 监狱】", fake.calls[2]["prompt"])
        self.assertEqual(len(c.history), 3)

    def _quiz_json(self, **over):
        d = {"question": "为什么公开处决被排斥？",
             "options": ["太花钱", "被看得和犯罪一样野蛮", "没人来看", "法律禁止"],
             "answer": 1,
             "hints": ["书里没提钱，回去看第一段", "对", "观众其实很多", "不是法律原因"],
             "explain": "原文说它的野蛮程度不亚于犯罪。",
             "quote": "这种惩罚方式，其野蛮程度不亚于，甚至超过犯罪本身"}
        d.update(over)
        return "好的：```json\n" + json.dumps(d, ensure_ascii=False) + "\n```"

    def test_quiz_four_choice_and_local_grade(self):
        fake = FakeClaude([self._quiz_json()])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        q = c.make_quiz(self.book.sections[0])
        self.assertNotIsInstance(q, str)
        self.assertEqual(len(q.options), 4)
        self.assertEqual(q.quote_loc, Loc(0, 6))
        n_calls = len(fake.calls)
        g = c.grade(q, 0)                       # 第一次错：给提示，不给答案
        self.assertFalse(g.correct)
        self.assertIn("回去看第一段", g.explanation)
        self.assertNotIn("被看得和犯罪一样野蛮", g.explanation)
        self.assertEqual(g.quote_loc, Loc(0, 6))
        self.assertFalse(q.finished)
        g = c.grade(q, 1)                       # 第二次对
        self.assertTrue(g.correct)
        self.assertTrue(q.finished)
        self.assertEqual(len(fake.calls), n_calls)   # 判题不调 AI
        with self.assertRaises(ValueError):
            c.grade(q, 1)

    def test_second_wrong_reveals_answer(self):
        fake = FakeClaude([self._quiz_json()])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        q = c.make_quiz(self.book.sections[0])
        c.grade(q, 0)
        g = c.grade(q, 2)
        self.assertFalse(g.correct)
        self.assertIn("正确答案是 B. 被看得和犯罪一样野蛮", g.explanation)
        self.assertTrue(q.finished)

    def test_bad_quiz_retried_then_skipped(self):
        bad = self._quiz_json(options=["a", "b", "c"])
        dup = self._quiz_json(options=["a", "a", "b", "c"])
        fake = FakeClaude([bad, dup])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        self.assertIsInstance(c.make_quiz(self.book.sections[0]), str)
        self.assertEqual(len(fake.calls), 2)
        fake = FakeClaude([bad, self._quiz_json(answer=3)])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        self.assertEqual(c.make_quiz(self.book.sections[0]).answer, 3)

    def test_quiz_skips_empty_section(self):
        fake = FakeClaude([])
        c = Companion(self.book, runner=fake.run, ask_once=fake.ask_once)
        sec = self.book.sections[0]
        sec._text = lambda: "太短"
        self.assertIsInstance(c.make_quiz(sec), str)
        self.assertEqual(fake.calls, [])

    def test_extract_json(self):
        self.assertEqual(extract_json('前言 {"a": 1} 后记'), {"a": 1})
        self.assertEqual(extract_json('```json\n{"a": 2}\n```'), {"a": 2})
        self.assertIsNone(extract_json("没有 JSON"))


if __name__ == "__main__":
    unittest.main()
