"""扫描版识别的纯逻辑部分 + 问图表带图（不调真的 OCR / AI）。"""

import tempfile
import unittest
from pathlib import Path

import pymupdf

from reader.ai.claude import Result
from reader.ai.companion import Companion, wants_page_image
from reader.books import Loc, open_book
from reader.books import ocr


class OcrLogicTests(unittest.TestCase):
    def test_join_cjk(self):
        self.assertEqual(ocr.join_cjk("生 活 与 文 学"), "生活与文学")
        self.assertEqual(ocr.join_cjk("种 子 ， 是 创 作"), "种子，是创作")
        self.assertEqual(ocr.join_cjk("the quick brown fox"), "the quick brown fox")
        self.assertEqual(ocr.join_cjk("用 MiSans 排 版"), "用 MiSans 排版")

    def test_guess_lang(self):
        self.assertEqual(ocr.guess_lang("作家任何时候都必须倾注心力"), "zh-Hans-CN")
        self.assertEqual(ocr.guess_lang("The next version is a variation"), "en-US")
        self.assertEqual(ocr.guess_lang("", "中国哲学简史"), "zh-Hans-CN")

    def test_store_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        import os
        os.environ["READER_DATA_DIR"] = str(tmp)
        try:
            pdf = tmp / "s.pdf"
            doc = pymupdf.open()
            doc.new_page()
            doc.save(pdf)
            st = ocr.OcrStore(pdf)
            st.put(0, {"text": "识别的字", "words": [[1, 2, 3, 4, "识", 0]]}, "zh-Hans-CN")
            st.save()
            again = ocr.OcrStore(pdf)
            self.assertEqual(again.page(0)["text"], "识别的字")
            self.assertEqual(again.lang, "zh-Hans-CN")
            reader_side = ocr.OcrStore(pdf)          # 读书这边先打开（此时只有第 0 页）
            self.assertIsNone(reader_side.page(1))
            writer = ocr.OcrStore(pdf)               # 后台识别是另一个对象在写
            writer.put(1, {"text": "后来识别的", "words": []}, "zh-Hans-CN")
            import time as _t
            _t.sleep(0.05)                           # 让文件修改时间变一下
            writer.save()
            self.assertEqual(reader_side.page(1)["text"], "后来识别的")   # 读书途中也用得上
            self.assertEqual(reader_side.page(0)["text"], "识别的字")
            b = open_book(pdf)                      # 空白页 = 扫描版
            self.assertFalse(b.has_text)
            if b.ocr is not None:                   # 装了识别才有
                self.assertEqual(b.page_text(0), "识别的字")
                self.assertEqual(b.page_words(0)[0][4], "识")
                self.assertEqual(b.text_source, "ocr")
        finally:
            os.environ.pop("READER_DATA_DIR", None)


class PageImageTests(unittest.TestCase):
    def test_wants_page_image(self):
        for q in ["这一页的图在演示什么", "这张示意图怎么理解", "What does this diagram show?", "表里第二列"]:
            self.assertTrue(wants_page_image(q), q)
        for q in ["作者的主要观点是什么", "这本书哪年出版"]:
            self.assertFalse(wants_page_image(q), q)

    def test_pdf_question_attaches_page(self):
        tmp = Path(tempfile.mkdtemp())
        pdf = tmp / "t.pdf"
        doc = pymupdf.open()
        for i in range(3):
            doc.new_page().insert_text((72, 72), f"Page {i + 1} text " * 20)
        doc.save(pdf)
        calls = []

        def run(prompt, *, system=None, resume=None, stop=None, images=None, **kw):
            calls.append(images)
            yield Result(text="好", session_id="s")

        c = Companion(open_book(pdf), runner=run)
        list(c.ask("这一页的图是什么意思", Loc(1)))
        list(c.ask("作者的观点呢", Loc(1)))
        self.assertTrue(calls[0] and calls[0][0][:4] == b"\x89PNG")
        self.assertIsNone(calls[1])


if __name__ == "__main__":
    unittest.main()
