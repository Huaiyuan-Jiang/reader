"""reader 的启动和总调度：书架 →「开始读」→ 读书界面（D 写的 ReadingScreen）→ 读书笔记页。

- `python -m reader`：打开书架（普通窗口）。
- 同一时间只有一个 reader：第二次启动把已有窗口叫到前面（QLocalServer「reader-single」）。
  正在读书时（锁屏中）再点，什么都不做：锁屏期间不能换书，读书窗口本来就在最上面。
- 启动顺序：theme.prepare_platform()（换 FreeType）→ QApplication → load_fonts → init_from_saved，
  然后在日志里记一行字体引擎和字体（字体出问题时一眼能查）。
- 启动时查一次上次是不是被强行关掉的（LockSession.recover），是就在书架顶上留一行字，不弹窗。
- 扫描版 PDF 没识别完文字的，启动后在后台接着识别（library.OcrQueue，一次一本，退出时停下）。

读书界面是另一个代理写的，这里**用到时才导入**：导入失败（还没写好 / 出错）就在书架顶上说一句，书架照常能用。
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from reader.library import Library
from reader.ui import theme

SERVER_NAME = "reader-single"
log = logging.getLogger("reader")


# ── 只开一个 ────────────────────────────────────────────────
def send_to_running(message: str, name: str = SERVER_NAME, timeout_ms: int = 800) -> bool:
    """已经有 reader 在跑：把消息（"show"）交给它，返回 True。"""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write((message + "\n").encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.waitForReadyRead(2000)        # 等对方回一声「收到」再断开（Windows 管道先断开会丢字）
    sock.disconnectFromServer()
    return True


class SingleInstance(QObject):
    """听着「reader-single」，收到一行字就回调 on_message(字)。"""

    def __init__(self, on_message: Callable[[str], None], name: str = SERVER_NAME, parent=None):
        super().__init__(parent)
        self.on_message = on_message
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        if not self.server.listen(name):
            QLocalServer.removeServer(name)      # 上次没退干净（非 Windows 会留下文件）
            self.server.listen(name)
        self.server.newConnection.connect(self._accept)

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            s = self.server.nextPendingConnection()
            s.readyRead.connect(lambda s=s: self._read(s))
            s.disconnected.connect(s.deleteLater)
            self._read(s)          # 连上之前就写完的字，readyRead 可能已经错过了

    def _read(self, s: QLocalSocket) -> None:
        while s.canReadLine():
            msg = bytes(s.readLine()).decode("utf-8", "replace").strip()
            if msg:
                s.write(b"ok\n")
                s.flush()
                self.on_message(msg)


# ── 读书界面（D 写的）───────────────────────────────────────
def load_reading_screen():
    """导入 ReadingScreen；导入不了返回 (None, 原因)。"""
    try:
        from reader.ui.reading import ReadingScreen
        return ReadingScreen, ""
    except Exception as e:  # noqa: BLE001
        log.exception("读书界面导入失败")
        return None, f"{type(e).__name__}: {e}"


class ReaderApp(QObject):
    """把书架、「开始读」、读书界面、读书笔记页串起来。screen_factory 给测试 / 演示换掉 ReadingScreen。"""

    def __init__(self, window, library: Library, screen_factory=None, parent=None):
        super().__init__(parent)
        self.win = window
        self.library = library
        self.screen_factory = screen_factory
        self.screen = None
        self.entry = None
        self.started: datetime | None = None
        window.start_requested.connect(self.begin)

    @property
    def reading(self) -> bool:
        return self.screen is not None

    def on_message(self, msg: str) -> None:
        """第二次启动传过来的 "show"：把书架叫到前面。读书中不理。"""
        if self.reading:
            return
        self.win.bring_to_front()

    def begin(self, entry, minutes: int) -> None:
        factory, why = (self.screen_factory, "") if self.screen_factory else load_reading_screen()
        if factory is None:
            self.win.add_notice(f"读书界面还没装好，这次开始不了（{why}）。书架照常能用。", kind="warn")
            return
        self.entry, self.started = entry, datetime.now()
        try:
            scr = factory(entry, minutes, self.library)
            self.screen = scr
            scr.finished.connect(self._finished)
            log.info("开始读《%s》%d 分钟", entry.title, minutes)
            self.win.hide()
            scr.start()
        except Exception as e:  # noqa: BLE001
            log.exception("读书界面打开失败")
            self.screen = None
            self.win.bring_to_front()
            self.win.add_notice(f"读书界面打开时出错了：{type(e).__name__}: {e}。这次没有开始计时。", kind="warn")

    def _finished(self, summary: dict) -> None:
        """读书界面收摊了（总结见 ReadingScreen.summary）。

        - open_failed：书没打开，这次不算，回书架说一句。
        - emergency_exit：不写笔记，回书架，顶上一行说记下了。
        - done：马上显示读书笔记页；笔记还在写（note_pending）时由笔记页在后台调 rewrite_note，
          写好了把路径补进这次读书的记录（书架上点这篇笔记能连着总结一起看）。
        """
        ended = datetime.now()
        entry, started = self.entry, self.started or ended
        summary = dict(summary or {})
        scr, self.screen = self.screen, None
        if scr is not None and hasattr(scr, "deleteLater"):
            QTimer.singleShot(0, scr.deleteLater)
        if summary.get("reason") == "open_failed":
            log.warning("书没打开：%s", summary.get("error"))
            self.win.show_shelf()
            self.win.bring_to_front()
            why = summary.get("error") or "这本书打不开"
            self.win.add_notice(f"{why}。这次没有开始计时。", kind="warn")
            return
        section = ""
        rec = None
        if entry is not None:
            try:
                section = self.library.place(entry.id).get("section", "")
                rec = self.library.log_session(entry.id, summary, started, ended, section=section)
            except Exception:  # noqa: BLE001
                log.exception("记这次读书失败")
        writer = summary.get("rewrite_note")
        if callable(writer) and rec is not None:
            lib = self.library

            def rewrite(writer=writer, rec=rec):
                path = writer()
                try:
                    lib.update_session(rec, note_path=str(path), note_error=None, note_pending=False)
                except Exception:  # noqa: BLE001
                    log.exception("把笔记路径记进这次读书失败")
                return path
            summary["rewrite_note"] = rewrite
        log.info("读完：%s", {k: v for k, v in summary.items() if not callable(v)})
        self.win.bring_to_front()
        if summary.get("reason") == "emergency_exit":
            self.win.show_shelf()
            m = summary.get("minutes")
            got = f"读过的 {m} 分钟照算，" if m else ""
            self.win.add_notice(f"这次走了紧急出口，提前结束了。{got}已经记进今天的阅读记录。")
        else:
            self.win.show_session(entry, summary, started, ended)


# ── 启动 ────────────────────────────────────────────────────
class _QuitOnClose(QObject):
    def __init__(self, ctl: ReaderApp, app: QApplication):
        super().__init__()
        self.ctl, self.app = ctl, app

    def eventFilter(self, obj, ev) -> bool:
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Close and not self.ctl.reading:
            QTimer.singleShot(0, self.app.quit)
        return False


def setup_logging() -> Path:
    path = theme.data_dir() / "reader.log"
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    return path


def forced_quit_notice() -> str | None:
    """上次读书被强行关掉：记下来（LockSession.recover），返回书架顶上那句话。"""
    try:
        from reader.lock import LockSession
        prev = LockSession().recover()
    except Exception:  # noqa: BLE001
        log.exception("检查上次是否被强行关掉时出错")
        return None
    if prev is None:
        return None
    when = datetime.fromtimestamp(prev.heartbeat)
    from reader.ui.shelf_kit import when_label
    mins = int(prev.credited_seconds // 60)
    got = f"读过的 {mins} 分钟照算，" if mins else ""
    log.info("上次被强行关掉：%s", prev)
    return f"上次读书（{when_label(when)}）是被强行关掉的。{got}这次也记进阅读记录了。"


def main(argv: list[str] | None = None, *, screen_factory=None, library: Library | None = None,
         server_name: str = SERVER_NAME, status_path: Path | None = None, recover: bool = True) -> int:
    argv = list(sys.argv if argv is None else argv)
    ap = argparse.ArgumentParser(prog="reader", description="强制阅读器")
    ap.parse_known_args(argv[1:])

    theme.prepare_platform()
    app = QApplication.instance() or QApplication(argv)
    if send_to_running("show", server_name):
        return 0
    app.setApplicationName("reader")
    app.setQuitOnLastWindowClosed(False)
    theme.load_fonts()
    theme.init_from_saved()
    setup_logging()
    log.info("启动 reader · 字体引擎 %s · 字体 %s", theme.engine_name(), theme.family())
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("reader.forced-reading")
        except Exception:  # noqa: BLE001
            pass

    from reader.ui.shelf import ShelfWindow
    lib = library or Library()
    kw = {"status_path": status_path} if status_path else {}
    win = ShelfWindow(lib, **kw)
    ctl = ReaderApp(win, lib, screen_factory)
    single = SingleInstance(ctl.on_message, server_name)
    notice = forced_quit_notice() if recover else None
    if notice:
        win.add_notice(notice)
    win.show()
    # 书架窗口关掉 = 退出。不用「最后一个窗口关了就退出」：读书界面换窗口的那一瞬间可能一个窗口都没有
    closer = _QuitOnClose(ctl, app)
    win.installEventFilter(closer)
    # 扫描版没识别完的，接着在后台识别（一次一本）；退出时停下，识别到的都存着，下次接着来
    QTimer.singleShot(1500, win.ocr.resume_all)
    app.aboutToQuit.connect(lambda: win.ocr.stop(timeout=3))
    app._reader = (win, ctl, single, closer)     # 留着引用
    return app.exec()


if __name__ == "__main__":
    # 演示：临时书架（放一本 ~/Downloads 里的真书）+ 一个假的读书界面：点「开始读」后
    # 不进锁屏，2 秒后直接发一份总结回来，显示读书笔记页。不碰真的书架和日报。
    import tempfile

    from PySide6.QtCore import Signal

    class DemoScreen(QObject):
        finished = Signal(dict)

        def __init__(self, entry, minutes, library):
            super().__init__()
            self.entry, self.minutes, self.library = entry, minutes, library

        def start(self):
            print(f"（演示）假装读《{self.entry.title}》{self.minutes} 分钟，不锁屏")
            note = self.library.save_note(self.entry.id, f"# 《{self.entry.title}》· 演示\n\n## 这次读了什么\n\n"
                                                         "- 这是演示用的笔记。\n", datetime.now())
            QTimer.singleShot(2000, lambda: self.finished.emit(
                {"minutes": self.minutes, "idle_deducted": 0, "pages": [1, 9], "quizzes": [],
                 "asked": 0, "highlights": 0, "note_path": str(note), "reason": "done"}))

    tmp = Path(tempfile.mkdtemp(prefix="reader-app-"))
    lib = Library(tmp / "data")
    src = next(iter((Path.home() / "Downloads").glob("*.epub")), None)
    if src:
        lib.add(src)
    sys.exit(main(sys.argv, screen_factory=DemoScreen, library=lib, server_name="reader-demo",
                  status_path=tmp / "status.json", recover=False))
