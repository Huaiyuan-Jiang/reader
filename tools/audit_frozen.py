"""Check packaged source correspondence and exclude user-data archive entries."""
from __future__ import annotations

import argparse
import json
import re
import types
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader


def normalize(code):
    return code.replace(co_filename='', co_consts=tuple(
        normalize(value) if isinstance(value, types.CodeType) else value
        for value in code.co_consts))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('exe', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = CArchiveReader(str(args.exe))
    pyz = next(archive.open_embedded_archive(name) for name, item in archive.toc.items() if item[-1] == 'z')
    forbidden = re.compile(r'(^|[/\\.])(_mimo_key|\.env|\.git|\.claude-reader|ai-secrets\.json|ai-config\.json|session\.json|status\.json|library\.json)([/\\.]|$)', re.I)
    issues = [name for name in [*archive.toc, *pyz.toc] if forbidden.search(name)]
    checked = 0
    for module in pyz.toc:
        if module != 'reader' and not module.startswith('reader.'):
            continue
        candidate = root.joinpath(*module.split('.'))
        source = candidate.with_suffix('.py')
        if not source.is_file():
            source = candidate / '__init__.py'
        if not source.is_file():
            issues.append(module + ': missing source')
            continue
        actual = pyz.extract(module)
        expected = compile(source.read_bytes(), str(source), 'exec', dont_inherit=True, optimize=0)
        if normalize(actual) != normalize(expected):
            issues.append(module + ': source mismatch')
        checked += 1
    if checked == 0:
        issues.append('No reader modules found')
    report = {'ok': not issues, 'checked_modules': checked, 'issues': issues}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
