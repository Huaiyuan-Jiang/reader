"""左边那条 AI：读书伙伴。

- 问答：一次读书（一次锁屏）一个对话，用 resume 续。系统提示只在第一轮给（书名、作者、目录）。
  每句提问前面带上「现在读到哪」；换了小节，就把这一节原文一起带上（只带一次）。
  它也保留自己的常识，可以讲书外的背景（SPEC）。
- 小测：读完一个小节，就这一节出 1 道 4 选 1（一次性调用，不进问答对话）。
  题目带一句**原文出处**（必须是原文里一字不差的一句），答错时界面跳到这句高亮。
- 判题：本地比对选项（出题时 AI 已经写好每个选项的提示），不再调 AI。
- 笔记：一次读书结束后写一页读书笔记（Markdown）。

所有调用都经 `runner`（默认 reader.ai.claude），测试里换成假的。
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..books import Book, Loc, Section
from . import claude, provider
from .claude import Delta, Result

SECTION_CHAR_LIMIT = 60000   # 一节正文最多带这么多字给 AI（再长就截断，说明截断了）

SYSTEM = """你是主人的读书伙伴，嵌在主人的阅读器左边。主人正在读下面这本书，你们用中文交流。

## 这本书
书名：{title}
作者：{author}
格式：{kind}

目录：
{toc}

## 你怎么说话
- 说中文，口语、清楚，先给结论再展开。
- 引用原文时保持原文原样，用「」括起来。
- 你手里有主人正在读的这一节的原文（每次换节会发给你）。书里的问题先以原文为准；
  原文没写的，可以用你自己的知识补充背景，但要说清楚「这是书外的补充」。
- 不剧透主人还没读到的部分，除非主人明确要求。
- 回答要短：一般三五句话；主人要细讲再展开。"""

QUIZ_SYSTEM = """你是阅读小测的出题人。只根据给你的这一节原文出一道 4 选 1 的题，中文。
只输出一个 JSON 对象，不要任何别的文字：
{"question": "题目",
 "options": ["选项A", "选项B", "选项C", "选项D"],
 "answer": 正确选项的序号（0～3）,
 "hints": ["选了A时给的话", "选了B时给的话", "选了C时给的话", "选了D时给的话"],
 "explain": "为什么正确答案是对的（一两句，引原文）",
 "quote": "原文里能找到答案的一句话，必须一字不差地从原文复制，20～80 字"}
要求：
- 考理解而不是考死记（不问页码、数字、人名拼写这种细节）；四个选项长短差不多，错误选项要像样、不能一眼排除。
- 正确答案的位置随机，不要总放在同一个位置。
- hints 里错误选项那条：说它错在哪、提示去原文哪里找，**但不要说出正确答案**（主人还有一次机会）。正确选项那条写一句简短的肯定。"""

NOTE_SYSTEM = """你帮主人写读书笔记，中文 Markdown。根据这次读的原文范围、主人的提问和小测情况，写一页笔记：
# 书名 · 日期
## 这次读了什么（三到五条要点）
## 我问过的问题（问题 + 一句话答案；没有就写「这次没提问」）
## 小测（对几道 / 共几道；错题写正确理解）
## 值得记住的原文（一两句，用「」）
## 留给下次（一个可以带着读下去的问题）
简洁，不要客套话。只输出笔记本身。"""


@dataclass
class Quiz:
    section: int
    question: str
    options: list[str]
    answer: int                 # 正确选项序号 0～3
    hints: list[str]            # 选了第 i 项时给的话（错项不透露答案）
    explain: str                # 正确答案为什么对
    quote: str
    quote_loc: Loc | None = None
    attempts: list[dict] = field(default_factory=list)   # [{"choice": i, "correct": bool}]

    MAX_TRIES = 2               # SPEC：答错讲解 + 再给一次机会

    @property
    def finished(self) -> bool:
        return any(a["correct"] for a in self.attempts) or len(self.attempts) >= self.MAX_TRIES

    @property
    def answer_text(self) -> str:
        return f"{'ABCD'[self.answer]}. {self.options[self.answer]}"


@dataclass
class Grade:
    correct: bool
    explanation: str
    quote: str = ""
    quote_loc: Loc | None = None
    error: str | None = None


def extract_json(text: str) -> dict | None:
    """从回答里抠出 JSON 对象（可能包在 ```json 里，也可能前后有字）。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cands = [m.group(1)] if m else []
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        cands.append(text[i:j + 1])
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


def parse_quiz(text: str, section_idx: int) -> Quiz | None:
    """校验 AI 出的题：4 个不重复的选项、答案序号在范围内。不合格返回 None（调用方重试）。"""
    d = extract_json(text)
    if not d:
        return None
    try:
        opts = [str(o).strip() for o in d.get("options", [])]
        ans = d.get("answer")
        if type(ans) is not int:
            return None
        hints = [str(h).strip() for h in d.get("hints", [])]
    except (TypeError, ValueError):
        return None
    q = str(d.get("question", "")).strip()
    if not q or len(opts) != 4 or not all(opts) or not 0 <= ans < 4 or len(set(opts)) != 4:
        return None
    hints = (hints + [""] * 4)[:4]
    return Quiz(section_idx, q, opts, ans, hints, str(d.get("explain", "")).strip(),
                str(d.get("quote", "")).strip())


# ── 在书里找出处 ────────────────────────────────────────────
_NORM = re.compile(r"[\s　，。、；：！？“”‘’「」『』（）()《》〈〉,.;:!?\"'\-—…·]+")


def _norm(s: str) -> str:
    return _NORM.sub("", s or "").lower()


def locate_quote(book: Book, section: Section, quote: str) -> Loc | None:
    """在这一节里找到 quote 所在的块（EPUB）或页（PDF）。找不到返回 None。

    先忽略标点和空白做精确包含；不行再按相似度取最像的一块（相似度太低就算找不到）。
    """
    q = _norm(quote)
    if len(q) < 4:
        return None
    units = _units(book, section)
    for loc, text in units:
        if q in _norm(text):
            return loc
    # 跨块 / 跨页：相邻两块拼起来找
    for (loc, a), (_, b) in zip(units, units[1:]):
        if q in _norm(a + b):
            return loc
    best, best_loc = 0.0, None
    for loc, text in units:
        t = _norm(text)
        if not t:
            continue
        sm = difflib.SequenceMatcher(None, q, t, autojunk=False)
        blk = sm.find_longest_match(0, len(q), 0, len(t))
        score = blk.size / len(q)
        if score > best:
            best, best_loc = score, loc
    return best_loc if best >= 0.6 else None


def _units(book: Book, section: Section) -> list[tuple[Loc, str]]:
    if book.kind == "epub":
        return [(loc, b.text) for loc, b in book.iter_blocks(section.start, section.end)
                if b.kind != "img"]
    return [(Loc(p), book.page_text(p)) for p in range(section.start.doc, section.end.doc)]


def section_payload(section: Section) -> str:
    text = section.text()
    if len(text) > SECTION_CHAR_LIMIT:
        text = text[:SECTION_CHAR_LIMIT] + "\n……（这一节太长，后面截掉了）"
    return text


# 问到这些词时，把当前 PDF 页的图片一起发给 Claude（扫描版问图表时也发图）
_VISUAL = re.compile(r"图|表|画面|示意|插图|照片|这一页|这页|版面|"
                     r"figure|fig\.|chart|diagram|table|graph|image|picture|photo|frame|shot|this page", re.I)


def wants_page_image(question: str, selection: str = "") -> bool:
    return bool(_VISUAL.search(question or ""))


class Companion:
    def __init__(self, book: Book, runner=None, ask_once=None):
        self.book = book
        self._run = runner or provider.run            # 有 MiMo 的 key 用 MiMo，没有用 Claude（见 provider）
        self._ask_once = ask_once or provider.ask_once
        self.session_id: str | None = None
        self.sent_sections: set[int] = set()
        self.history: list[dict] = []          # [{"q":..., "a":..., "selection":..., "section":...}]
        self.stop = threading.Event()

    def system_prompt(self) -> str:
        return SYSTEM.format(title=self.book.title, author=self.book.author or "（不详）",
                             kind="PDF（按页）" if self.book.kind == "pdf" else "EPUB",
                             toc=self.book.toc_outline() or "（这本书没有目录）")

    def where(self, loc: Loc) -> str:
        sec = self.book.section_at(loc)
        pos = f"第 {loc.doc + 1} 页" if self.book.kind == "pdf" else ""
        name = sec.title if sec else "（不在任何小节里）"
        return f"现在读到：{name}" + (f"，{pos}" if pos else "")

    def build_message(self, question: str, loc: Loc, selection: str = "") -> str:
        parts = [f"【{self.where(loc)}】"]
        sec = self.book.section_at(loc)
        if sec is not None and sec.idx not in self.sent_sections:
            body = section_payload(sec)
            if body.strip():
                parts.append(f"【这一节的原文：{sec.title}】\n{body}\n【原文结束】")
            else:
                parts.append("【这一节没有可读的文字（可能是扫描版），只能根据目录和你的知识回答】")
        if selection.strip():
            parts.append(f"【主人在书上选中了这段】\n「{selection.strip()}」")
        parts.append(f"【主人问】{question.strip()}")
        return "\n\n".join(parts)

    def page_image(self, loc: Loc, question: str) -> bytes | None:
        """PDF 上问到图表 / 画面 / 这一页：带上当前页的图片。EPUB 不带（图片块另说）。"""
        if self.book.kind != "pdf" or not wants_page_image(question):
            return None
        try:
            return self.book.page_png(loc.doc)
        except Exception:  # noqa: BLE001
            return None

    def ask(self, question: str, loc: Loc, selection: str = "") -> Iterator[Delta | Result]:
        """流式回答。最后一个一定是 Result。"""
        self.stop.clear()
        sec = self.book.section_at(loc)
        msg = self.build_message(question, loc, selection)
        png = self.page_image(loc, question)
        if png is not None:
            msg += f"\n\n【附上了主人正在看的第 {loc.doc + 1} 页的图片】"
        kw = {"images": [png]} if png is not None else {}
        final = Result(error="没有结果")
        for ev in self._run(msg, system=None if self.session_id else self.system_prompt(),
                            resume=self.session_id, stop=self.stop, **kw):
            if isinstance(ev, Result):
                final = ev
            yield ev
        if final.ok:
            self.session_id = final.session_id or self.session_id
            if sec is not None:
                self.sent_sections.add(sec.idx)
        elif self.session_id and "resume" in (final.error or "").lower():
            # 续不上（会话丢了）：下一问重新开，原文要重发
            self.session_id = None
            self.sent_sections.clear()
        self.history.append({"q": question, "a": final.text, "selection": selection,
                             "section": sec.idx if sec else None, "error": final.error})

    # ── 小测 ────────────────────────────────────────────
    def make_quiz(self, section: Section) -> Quiz | str:
        """出题。成功返回 Quiz，失败返回一句给界面看的中文原因。"""
        body = section_payload(section)
        if len(body.strip()) < 200:
            return "这一节没有足够的文字（可能是扫描版或插图页），这次不出题"
        prompt = f"书名：{self.book.title}\n小节：{section.title}\n\n【原文】\n{body}\n【原文结束】\n\n出一道题。"
        for _ in range(2):
            res = self._ask_once(prompt, system=QUIZ_SYSTEM)
            if not res.ok:
                return res.error or "出题失败"
            q = parse_quiz(res.text, section.idx)
            if q is not None:
                q.quote_loc = locate_quote(self.book, section, q.quote)
                return q
        return "AI 出的题格式不对，这次先跳过"

    @staticmethod
    def grade(quiz: Quiz, choice: int) -> Grade:
        """本地判题（4 选 1 不用再问 AI）。

        第一次错：给这一项的提示（不透露答案）+ 原文位置，再给一次机会。
        第二次还错：直接给正确答案和原因（界面据此接着读，记进笔记）。
        """
        if quiz.finished:
            raise ValueError("这道题已经答完了")
        correct = choice == quiz.answer
        quiz.attempts.append({"choice": choice, "correct": correct})
        if correct:
            text = quiz.hints[choice] or "答对了。"
            return Grade(True, f"{text} {quiz.explain}".strip(), quiz.quote, quiz.quote_loc)
        if len(quiz.attempts) < quiz.MAX_TRIES:
            return Grade(False, quiz.hints[choice] or "不太对，看一眼原文再选一次。",
                         quiz.quote, quiz.quote_loc)
        return Grade(False, f"正确答案是 {quiz.answer_text}。{quiz.explain}",
                     quiz.quote, quiz.quote_loc)

    # ── 笔记 ────────────────────────────────────────────
    def write_note(self, *, date: str, start: Loc, end: Loc, quizzes: list[Quiz],
                   minutes: int) -> Result:
        text = self.book.text_between(start, end)
        if len(text) > SECTION_CHAR_LIMIT:
            text = text[:SECTION_CHAR_LIMIT] + "\n……（后面截掉了）"
        qa = "\n".join(f"- 问：{h['q']}\n  答：{(h['a'] or '').strip()[:400]}"
                       for h in self.history if h.get("a")) or "（这次没提问）"
        qz = "\n".join(
            f"- 题：{q.question}\n  正确答案：{q.answer_text}\n" +
            "\n".join(f"  第 {i + 1} 次选：{'ABCD'[a['choice']]}. {q.options[a['choice']]} → "
                      f"{'对' if a['correct'] else '错'}" for i, a in enumerate(q.attempts))
            for q in quizzes) or "（这次没有小测）"
        prompt = (f"书名：{self.book.title}\n日期：{date}\n这次读了 {minutes} 分钟\n\n"
                  f"【这次读的原文】\n{text}\n【原文结束】\n\n【主人的提问】\n{qa}\n\n【小测】\n{qz}")
        return self._ask_once(prompt, system=NOTE_SYSTEM, timeout=300)
