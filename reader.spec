# Build from an isolated environment; never collect local data or credentials.
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# Restrict native dependency discovery to this Python environment and Windows.
windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
os.environ['PATH'] = os.pathsep.join(map(str, [Path(sys.prefix) / 'Scripts',
    Path(sys.base_prefix), Path(sys.base_prefix) / 'DLLs', windows / 'System32', windows]))
hidden = collect_submodules('reader') + collect_submodules('winrt')
metadata = []
metadata.append(('docs/guide.html', 'docs'))
for package in ('claude-agent-sdk', 'mcp', 'mcp-types', 'jsonschema', 'referencing'):
    metadata += copy_metadata(package)
a = Analysis(
    ['tools/frozen_entry.py'], pathex=[SPECPATH], binaries=[], datas=metadata,
    hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['reader.ai._mimo_key', 'tests', 'pytest', 'pip_audit',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtWebEngineQuick', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
              'tkinter', 'matplotlib', 'IPython'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
# Qt uses Windows' ICU API. A foreign ICU DLL on PATH can have incompatible
# exports; leave this OS component to Windows instead of packaging it.
a.binaries = [item for item in a.binaries if item[0].lower() != 'icuuc.dll']
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='reader',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
