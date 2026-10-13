"""生成没有本机凭据和阅读数据的源码包，不访问 GitHub。"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP = {'README.md', 'requirements.txt', '.gitignore', '.gitattributes', '.env.example',
       'ai-config.example.json', '打开reader.bat', 'LICENSE', 'reader.spec', 'requirements-build.txt', 'THIRD_PARTY_NOTICES.md'}
DOCS = {'AI_CONFIGURATION.md', 'HARNESS.md', 'PUBLISHING.md', 'SECURITY_REVIEW.md',
        'GETTING_STARTED.md', 'guide.html', 'VALIDATION_2026-10-13.md'}
PATTERNS = [re.compile(r'\bsk-(?:ant-)?[A-Za-z0-9_-]{24,}'),
            re.compile(r'(?i)(?:api[_-]?key|access[_-]?token)\s*[=:]\s*[\x22\x27][A-Za-z0-9_-]{24,}')]


def source_files():
    paths = [ROOT / n for n in TOP if (ROOT / n).is_file()]
    for dirname in ('reader', 'tests'):
        paths.extend(p for p in (ROOT / dirname).rglob('*.py')
                     if p.name != '_mimo_key.py' and '__pycache__' not in p.parts)
    paths.extend(ROOT / 'tools' / name for name in ('package_source.py', 'frozen_entry.py'))
    paths.extend(ROOT / 'docs' / n for n in DOCS if (ROOT / 'docs' / n).is_file())
    for name in ('shelf.png', 'start.png', 'ai-settings.png', 'reading.png', 'exit.png'):
        asset = ROOT / 'docs' / 'guide-assets' / name
        if asset.is_file():
            paths.append(asset)
    return sorted(set(paths))


def package(output: Path):
    paths = source_files()
    for p in paths:
        if p.suffix == '.png':
            continue
        text = p.read_text(encoding='utf-8-sig')
        if any(pattern.search(text) for pattern in PATTERNS):
            raise RuntimeError(f'疑似凭据，请检查文件：{p.relative_to(ROOT)}')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for p in paths:
            archive.write(p, 'reader/' + p.relative_to(ROOT).as_posix())
    print(f'已生成 {output.resolve()}，{len(paths)} 个源文件；不含本机凭据或阅读数据。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    package(parser.parse_args().output)
