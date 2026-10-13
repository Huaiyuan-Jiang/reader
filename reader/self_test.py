"""Offline executable validation with generated fixtures and temporary data."""
from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import traceback
import zipfile
from pathlib import Path


def run(output: str) -> int:
    result = {'ok': False, 'checks': []}
    try:
        with tempfile.TemporaryDirectory(prefix='reader-self-test-') as temp:
            root = Path(temp)
            os.environ.update(READER_NO_LOCK='1', READER_DATA_DIR=str(root / 'data'),
                              QT_QPA_PLATFORM='offscreen', READER_AI='custom')
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import QTimer
            app = QApplication.instance() or QApplication([])
            import pymupdf
            pdf = root / 'sample.pdf'
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_text((72, 72), 'Reader offline validation')
            doc.save(pdf)
            doc.close()
            from reader.books import open_book
            book = open_book(pdf)
            assert book is not None
            book.doc.close()
            result['checks'].append('pdf-open')
            from reader.ui.bookview._render import render, worker
            doc = pymupdf.open(pdf)
            w, h, pixels = render(doc, 0, 240, None)
            assert w == 240 and len(pixels) == w * h * 4
            doc.close()
            result['checks'].append('pdf-render-numpy')
            ctx = multiprocessing.get_context('spawn')
            request_parent, request_child = ctx.Pipe()
            response_parent, response_child = ctx.Pipe()
            proc = ctx.Process(target=worker, args=(str(pdf), request_child, response_child))
            proc.start()
            try:
                request_parent.send(('want', [(0, 240, None)]))
                assert response_parent.poll(30), 'PDF child did not respond'
                response = response_parent.recv()
                assert response[0] == 'img', str(response[:4])
                request_parent.send(('quit',))
                proc.join(10)
                assert proc.exitcode == 0, 'PDF child failed to exit'
            finally:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(5)
                for pipe in (request_parent, request_child, response_parent, response_child):
                    pipe.close()
            result['checks'].append('pdf-multiprocessing')
            epub = root / 'sample.epub'
            with zipfile.ZipFile(epub, 'w') as z:
                z.writestr('META-INF/container.xml', '<container><rootfile full-path="book.opf"/></container>')
                z.writestr('book.opf', '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
                           '<metadata><dc:title>Sample</dc:title></metadata><manifest>'
                           '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
                           '</manifest><spine><itemref idref="one"/></spine></package>')
                z.writestr('one.xhtml', '<html><body><h1>Sample</h1><p>Offline test.</p></body></html>')
            epub_book = open_book(epub)
            assert epub_book.title == 'Sample'
            epub_book.zip.close()
            result['checks'].append('epub-open')
            from reader.ai import config, secrets, api, harness
            config.validate(config.defaults())
            secrets.put('custom', 'offline-fixture-key')
            assert secrets.get('custom') == 'offline-fixture-key'
            assert 'offline-fixture-key' not in (config.data_dir() / 'ai-secrets.json').read_text('utf-8')
            secrets.put('custom', '')
            result['checks'].append('dpapi-and-ai-imports')
            from claude_agent_sdk import ClaudeAgentOptions
            ClaudeAgentOptions(tools=[], setting_sources=[])
            result['checks'].append('claude-sdk-import')
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.graphics.imaging import BitmapDecoder
            from winrt.windows.storage.streams import DataWriter
            from winrt.windows.globalization import Language
            result['checks'].append('windows-ocr-imports')
            from reader.library import Library
            from reader.ui.shelf import ShelfWindow
            from reader.ui.theme import load_fonts, init_from_saved
            load_fonts()
            init_from_saved()
            window = ShelfWindow(Library(), check_login=False)
            window.show()
            QTimer.singleShot(300, app.quit)
            app.exec()
            window.close()
            result['checks'].append('shelf-ui')
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
