#!/usr/bin/env python
"""把 GroupSpeakerTag/ 打成 .lbpkg。

用法：
    python build-lbpkg.py 0.6.0
不传版本号时，从 manifest.yaml 读取。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'GroupSpeakerTag')


def read_version() -> str:
    with open(os.path.join(SRC, 'manifest.yaml'), encoding='utf-8') as handle:
        text = handle.read()
    match = re.search(r'^\s*version:\s*([\w.\-]+)\s*$', text, re.MULTILINE)
    if not match:
        raise SystemExit('manifest.yaml 里找不到 version')
    return match.group(1)


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else read_version()
    out = os.path.join(ROOT, f'GroupSpeakerTag-{version}.lbpkg')

    entries = []
    for base, dirs, names in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in names:
            if name.endswith('.pyc'):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, SRC).replace(os.sep, '/')
            entries.append((rel, full))
    entries.sort()

    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for rel, full in entries:
            archive.write(full, rel)

    data = open(out, 'rb').read()
    print(f'打包 {version}: {len(data)/1024:.1f} KB, {len(entries)} 条目')
    for rel, _ in entries:
        print('  ' + rel)
    print('sha256:', hashlib.sha256(data).hexdigest())
    print('输出:', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
