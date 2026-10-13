"""外观：两套颜色（暗 / 亮）、MiSans、FreeType。所有界面文件都从这里取颜色和字体。

## 颜色
直接取自界面稿 docs/design/index.html 的 `.t-dark` / `.t-light`（Claude 风格的配色；
暗色取样自 Claude 桌面版）。键名跟稿子里的 CSS 变量一一对应，改颜色两边一起改。

## 字体（2026-09-23 在屏幕上比过定的）
- MiSans 官方分字重（Thin…Heavy 十个 ttf）。FreeType 挑不动可变字体 MiSans VF 的字重，所以用分字重。
  字重编号：Regular 330、Medium 380、Semibold 520、Bold 630 —— 代码里要 400 拿到 Medium、600 拿到 Bold。
- 字体文件不进仓库（80 MB）：放进 `%LOCALAPPDATA%\\reader\\fonts\\MiSans\\`。
- `prepare_platform()` 在创建 QApplication **之前**调：Windows 上换成 FreeType、不做 hinting，
  边缘是灰度过渡，没有 ClearType 的红蓝彩边（2026-09-24 用真书对比过）。
  退回系统渲染：环境变量 READER_FONT_ENGINE=system。

## 用法
    theme.prepare_platform(); app = QApplication(...); theme.load_fonts(); theme.apply_app(app)
    theme.style(widget, "QLabel {{ color: {t2}; }}")   # {键名} 换成当前主题的颜色
    theme.on_change(lambda name: ...)                    # 换主题时回调
"""

from __future__ import annotations

import json
import os
import re
import sys
import weakref
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

# ── 颜色（照抄界面稿） ────────────────────────────────────────
DARK = {
    "desk": "#0F0F0F", "bg0": "#1B1B1B", "bg1": "#111111", "bg2": "#151515", "bg3": "#212121",
    "bg4": "#2D2C2C", "bg5": "#343434", "l1": "#262626", "l2": "#333333", "l3": "#474644",
    "t1": "#F0EFEC", "t2": "#C3C2B8", "t3": "#A6A49C", "t4": "#898782", "t5": "#666561",
    "acc": "#CC7C5E", "acc_ink": "#FFFFFF", "pri": "#CC7C5E", "pri_ink": "#FFFFFF", "focus": "#898782",
    "red": "#C4675A", "redt": "#DE8F80", "green": "#7FA06A", "greent": "#A3C28E",
    # 书页
    "paper": "#1A1917", "ink": "#DAD6CC", "ink2": "#9C988E",
    "pdfdesk": "#0C0C0C", "pdfpaper": "#C9C2B3", "pdfink": "#221F1B", "pdfink2": "#5A5449",
    # 标记（带透明度，用 color() 取 QColor）
    "hl": "rgba(222,184,92,.30)", "pdfhl": "rgba(214,160,40,.45)",
    "src": "rgba(204,124,94,.22)", "sel": "rgba(138,174,214,.34)",
    "scrim": "rgba(0,0,0,.58)", "shadow": "rgba(0,0,0,.6)",
    # 副屏遮罩（两种主题都用深色）
    "mon": "#0A0A0A", "mon_t": "#8D8A83", "mon_n": "#C9C6BE",
}
LIGHT = {
    "desk": "#E8E6DC", "bg0": "#FFFFFF", "bg1": "#F5F4ED", "bg2": "#FAF9F5", "bg3": "#FFFFFF",
    "bg4": "#F0EEE6", "bg5": "#E8E6DC", "l1": "#E8E6DC", "l2": "#D9D6CB", "l3": "#BEBAAD",
    "t1": "#141413", "t2": "#3D3D3A", "t3": "#5E5D59", "t4": "#73726C", "t5": "#9A988F",
    "acc": "#C96442", "acc_ink": "#FFFFFF", "pri": "#141413", "pri_ink": "#FAF9F5", "focus": "#141413",
    "red": "#B8432C", "redt": "#A53C27", "green": "#5A7A45", "greent": "#4D6B3A",
    "paper": "#F8F5EC", "ink": "#22201C", "ink2": "#6A655B",
    "pdfdesk": "#E4E1D7", "pdfpaper": "#FFFDF8", "pdfink": "#1E1C19", "pdfink2": "#6E685D",
    "hl": "rgba(242,200,80,.45)", "pdfhl": "rgba(242,200,80,.55)",
    "src": "rgba(201,100,66,.15)", "sel": "rgba(58,110,165,.22)",
    "scrim": "rgba(40,30,20,.30)", "shadow": "rgba(40,25,10,.20)",
    "mon": "#12110F", "mon_t": "#8A857B", "mon_n": "#CFC9BC",
}
THEMES = {"dark": DARK, "light": LIGHT}

# 书页排版（界面稿板 3：EPUB 18px、行高 1.85、每行约 34 字、段首空两格）
BOOK_PX = 18
BOOK_LINE = 1.85
BOOK_COLUMN_EM = 34
# Qt 的比例行高是乘在字体自然行距上的（MiSans 约 1.33），CSS 的 1.85 要换算成这个
MISANS_NATURAL = 1.33

FALLBACK_FAMILY = "Microsoft YaHei UI"


def data_dir() -> Path:
    d = Path(os.environ.get("READER_DATA_DIR") or
             Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "reader")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 主题选择 ─────────────────────────────────────────────────
class _Bus(QObject):
    changed = Signal(str)


_bus: _Bus | None = None
_current = "dark"


def bus() -> _Bus:
    global _bus
    if _bus is None:
        _bus = _Bus()
    return _bus


def _settings_path() -> Path:
    return data_dir() / "ui.json"


def saved_choice() -> str:
    try:
        v = json.loads(_settings_path().read_text("utf-8")).get("theme", "dark")
    except (OSError, ValueError):
        v = "dark"
    return v if v in ("dark", "light", "system") else "dark"


def windows_prefers_light() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return bool(winreg.QueryValueEx(k, "AppsUseLightTheme")[0])
    except OSError:
        return False


def current() -> str:
    return _current


def is_light() -> bool:
    return _current == "light"


def set_theme(choice: str, *, save: bool = True) -> None:
    global _current
    name = ("light" if windows_prefers_light() else "dark") if choice == "system" else choice
    if name not in THEMES:
        name = "dark"
    changed = name != _current
    _current = name
    if save:
        try:
            p = _settings_path()
            data = json.loads(p.read_text("utf-8")) if p.exists() else {}
            data["theme"] = choice
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        except (OSError, ValueError):
            pass
    app = QApplication.instance()
    if app is not None:
        apply_app(app)
    if changed:
        bus().changed.emit(name)


def on_change(cb: Callable[[str], None]) -> None:
    bus().changed.connect(cb)


def tok(key: str, theme: str | None = None) -> str:
    return THEMES[theme or _current][key]


_RGBA = re.compile(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)")


def color(key: str, alpha: float | None = None, theme: str | None = None) -> QColor:
    v = tok(key, theme)
    m = _RGBA.fullmatch(v.replace(" ", ""))
    c = QColor(int(m[1]), int(m[2]), int(m[3])) if m else QColor(v)
    if m:
        c.setAlphaF(float(m[4]))
    if alpha is not None:
        c.setAlphaF(alpha)
    return c


def mix(a: str, b: str, amount: float) -> str:
    """CSS color-mix：amount 份 a + (1-amount) 份 b，返回 #RRGGBB。键名或 #hex 都行。"""
    ca = QColor(tok(a)) if not a.startswith("#") else QColor(a)
    cb = QColor(tok(b)) if not b.startswith("#") else QColor(b)
    r = lambda x, y: round(x * amount + y * (1 - amount))  # noqa: E731
    return QColor(r(ca.red(), cb.red()), r(ca.green(), cb.green()), r(ca.blue(), cb.blue())).name()


def qss(template: str, theme: str | None = None) -> str:
    """把 {键名} 换成颜色。模板里真正的花括号写成 {{ }}。"""
    return template.format(**THEMES[theme or _current])


def style(widget: QWidget, template: str) -> None:
    """套样式，并且换主题时自动重套。"""
    widget.setStyleSheet(qss(template))
    ref = weakref.ref(widget)

    def _again(_name: str) -> None:
        w = ref()
        if w is not None:
            try:
                w.setStyleSheet(qss(template))
            except RuntimeError:   # C++ 对象已删
                pass
    on_change(_again)


# ── 字体 ─────────────────────────────────────────────────────
_family = FALLBACK_FAMILY
_freetype = False


def _font_dirs() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return [local / "reader" / "fonts" / "MiSans"]


def static_misans() -> list[Path]:
    need = {"MiSans-Regular.ttf", "MiSans-Medium.ttf", "MiSans-Semibold.ttf", "MiSans-Bold.ttf"}
    for d in _font_dirs():
        files = sorted(d.glob("MiSans-*.ttf")) if d.is_dir() else []
        if need <= {p.name for p in files}:
            return files
    return []


def prepare_platform() -> bool:
    """创建 QApplication 之前调。条件满足就换成 FreeType 画字，返回是否换了。"""
    if sys.platform != "win32" or os.environ.get("READER_FONT_ENGINE", "").lower() == "system":
        return False
    if not static_misans():
        return False
    if os.environ.get("QT_QPA_PLATFORM", "").strip() not in ("", "windows"):
        return False   # offscreen 之类（测试）原样不动
    os.environ["QT_QPA_PLATFORM"] = "windows:fontengine=freetype"
    return True


def engine_name() -> str:
    app = QGuiApplication.instance()
    return ("freetype" if app is not None and app.platformName() == "windows"
            and "fontengine=freetype" in os.environ.get("QT_QPA_PLATFORM", "") else "system")


def load_fonts() -> str:
    """装 MiSans。找不到就用微软雅黑 UI，不报错。返回用上的字体名。"""
    global _family, _freetype
    _freetype = engine_name() == "freetype"
    for p in static_misans():
        fid = QFontDatabase.addApplicationFont(str(p))
        if fid >= 0 and "MiSans" in QFontDatabase.applicationFontFamilies(fid):
            _family = "MiSans"
    if _family != "MiSans" and "MiSans" in QFontDatabase.families():
        _family = "MiSans"
    return _family


def family() -> str:
    return _family


def font(px: int = 14, weight: int = 400, *, tnum: bool = False) -> QFont:
    f = QFont(_family)
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(weight))
    if _freetype:
        f.setHintingPreference(QFont.PreferNoHinting)
    if tnum:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError):
            pass
    return f


def book_line_height_percent() -> float:
    """QTextBlockFormat.setLineHeight(…, ProportionalHeight) 要的值（CSS 1.85 → Qt 约 139%）。"""
    return BOOK_LINE / MISANS_NATURAL * 100


# ── 书页字体（EPUB 的 Aa 里选：黑体 / 宋体 / 楷体） ─────────────────────
# 黑体就是 MiSans。宋体、楷体用电脑上有的（不进仓库）：宋体要思源宋体（Noto Serif SC / Source Han Serif），
# 系统自带的华文宋体、中易宋体在屏幕上太细太虚，没有思源宋体就不给「宋体」这一项；楷体优先霞鹜文楷，
# 没有就用系统的楷体。2026-09-25 在屏幕上比过：思源宋体的可变字体不设 wght 轴会拿到 ExtraLight（发虚），
# 所以可变字体一律用 setVariableAxis 定字重。
BOOK_FACES = {"sans": "黑体", "serif": "宋体", "kai": "楷体"}
_FACE_FILES = {
    "serif": ["NotoSerifSC-VF.ttf", "SourceHanSerifSC-VF.ttf", "SourceHanSerifSC-VF.otf",
              "NotoSerifSC-Regular.otf", "NotoSerifSC-Regular.ttf", "SourceHanSerifSC-Regular.otf",
              "NotoSerifCJKsc-Regular.otf"],
    "kai": ["LXGWWenKaiScreen.ttf", "LXGWWenKai-Regular.ttf", "LXGWWenKaiGB-Regular.ttf",
            "simkai.ttf", "STKAITI.TTF"],
}
_FACE_FAMILIES = {   # 已经装在系统里（按名字找得到）的也算
    "serif": ["Noto Serif SC", "Source Han Serif SC", "思源宋体", "Noto Serif CJK SC"],
    "kai": ["LXGW WenKai Screen", "LXGW WenKai", "霞鹜文楷", "KaiTi", "楷体", "STKaiti"],
}
# 三档粗细（细 / 标准 / 粗）和标题的字重。MiSans 用分字重：Regular 330、Medium 380、Demibold 450、Bold 630；
# 楷体只有一个字重，「粗」让 FreeType 加粗
_FACE_WEIGHTS = {"sans": (330, 380, 450, 600), "serif": (400, 500, 600, 700), "kai": (400, 400, 700, 700)}
_faces: dict[str, tuple[str, bool] | None] = {}


def _face_dirs() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    return [local / "reader" / "fonts", local / "Microsoft" / "Windows" / "Fonts", win]


def book_face(key: str) -> tuple[str, bool] | None:
    """(字体名, 是不是可变字体)；这台电脑上没有就是 None。第一次用到时才装字体文件。"""
    if key == "sans":
        return _family, False
    if key in _faces:
        return _faces[key]
    got = None
    for d in _face_dirs():
        for name in _FACE_FILES.get(key, []):
            p = d / name
            if not p.is_file():
                continue
            fid = QFontDatabase.addApplicationFont(str(p))
            fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
            if fams:
                got = (fams[0], "VF" in name or "[wght" in name)
                break
        if got:
            break
    if got is None:
        have = set(QFontDatabase.families())
        fam = next((f for f in _FACE_FAMILIES.get(key, []) if f in have), None)
        got = (fam, False) if fam else None
    _faces[key] = got
    return got


def book_faces() -> list[str]:
    """这台电脑上能用的书页字体（按 BOOK_FACES 的顺序）。"""
    return [k for k in BOOK_FACES if book_face(k) is not None]


def book_font(px: int, face: str = "sans", level: int = 1) -> QFont:
    """书页正文的字：level 0 细、1 标准、2 粗、3 标题。字体没有就退回黑体。"""
    got = book_face(face)
    if got is None:
        face, got = "sans", book_face("sans")
    fam, variable = got
    w = _FACE_WEIGHTS[face][max(0, min(3, level))]
    if face == "sans":
        return font(px, w)
    f = QFont(fam)
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(w))
    if variable:
        try:
            f.setVariableAxis(QFont.Tag("wght"), float(w))
        except (AttributeError, TypeError):
            pass
    if _freetype:
        f.setHintingPreference(QFont.PreferNoHinting)
    return f


def natural_line(f: QFont) -> float:
    """这个字的自然行距是字号的几倍（Qt 的比例行高乘在它上面；MiSans 约 1.33，思源宋体约 1.44）。"""
    from PySide6.QtGui import QFontMetricsF
    px = f.pixelSize() if f.pixelSize() > 0 else 16
    return max(0.8, QFontMetricsF(f).lineSpacing() / px)


# ── 应用级 ───────────────────────────────────────────────────
_BASE = """
QWidget {{ color: {t1}; }}
QToolTip {{ background: {bg3}; color: {t2}; border: 1px solid {l2}; padding: 4px 8px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {l2}; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {l3}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {l2}; border-radius: 4px; min-width: 28px; }}
QPushButton {{ height: 34px; padding: 0 14px; border-radius: 8px; background: {bg4};
  border: 1px solid {l2}; color: {t1}; font-size: 13px; }}
QPushButton:hover {{ background: {bg5}; }}
QPushButton:disabled {{ color: {t5}; }}
QPushButton[kind="pri"] {{ background: {pri}; border-color: {pri}; color: {pri_ink}; font-weight: 600; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; color: {t3}; }}
QPushButton[kind="ghost"]:hover {{ background: {bg4}; }}
QPushButton[kind="danger"] {{ color: {redt}; font-weight: 600; }}
QLabel[kind="cap"] {{ color: {t4}; font-size: 12px; }}
QLabel[kind="lbl"] {{ color: {t4}; font-size: 12px; letter-spacing: 1px; }}
"""


def apply_app(app: QApplication) -> None:
    app.setFont(font(14))
    app.setStyleSheet(qss(_BASE))


def titlebar(widget: QWidget) -> None:
    """普通窗口的系统标题栏跟主题走（Windows 11）。失败就算了。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(0 if is_light() else 1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        for attr, key in ((35, "bg1"), (36, "t3")):
            c = QColor(tok(key))
            ref = ctypes.c_int(c.red() | (c.green() << 8) | (c.blue() << 16))
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref))
    except Exception:  # noqa: BLE001
        pass


def init_from_saved() -> None:
    set_theme(saved_choice(), save=False)
