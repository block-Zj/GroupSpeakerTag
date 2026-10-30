#!/usr/bin/env python
"""校验 .lbpkg：插件清单 + 每个组件清单（EventListener / Tool ...）。

用法（必须用 LangBot 自己的解释器跑，才有 langbot / langbot_plugin 依赖）：
    "<langbot-env>/Scripts/python.exe" validate-lbpkg.py GroupSpeakerTag-0.6.0.lbpkg
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import zipfile

import yaml


def find_langbot_pkg() -> str:
    """从当前解释器的 site-packages 里找 langbot。"""

    for path in sys.path:
        candidate = os.path.join(path, 'langbot', 'pkg', 'plugin', 'archive.py')
        if os.path.exists(candidate):
            return candidate
    raise SystemExit('找不到 langbot 包，请用 LangBot 的解释器运行本脚本')


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit('用法: validate-lbpkg.py <package.lbpkg>')
    pkg = sys.argv[1]

    spec = importlib.util.spec_from_file_location('lb_archive', find_langbot_pkg())
    module = importlib.util.module_from_spec(spec)
    sys.modules['lb_archive'] = module
    spec.loader.exec_module(module)

    raw = open(pkg, 'rb').read()
    inspected = module.inspect_plugin_archive(raw)
    manifest = inspected.manifest
    meta = manifest['metadata']

    print(f"author/name : {meta['author']} / {meta['name']}")
    print(f"version     : {meta['version']}")
    print(f"config      : {[c['name'] for c in manifest['spec'].get('config', [])]}")
    print(f"components  : {list(manifest['spec']['components'].keys())}")
    print(f"cert state  : {inspected.certificate.state}")

    # 逐个组件清单做结构校验
    from langbot_plugin.api.definition.components.manifest import ComponentManifest

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(pkg) as archive:
            archive.extractall(tmp)
        for root, _, names in os.walk(os.path.join(tmp, 'components')):
            for name in names:
                if not name.endswith('.yaml'):
                    continue
                full = os.path.join(root, name)
                data = yaml.safe_load(open(full, encoding='utf-8'))
                rel = os.path.relpath(full, tmp)
                try:
                    comp = ComponentManifest(owner=meta['author'], manifest=data, rel_path=rel)
                    print(f"  OK   {rel}  kind={comp.kind}  name={comp.metadata.name}")
                except Exception as exc:
                    failures += 1
                    print(f"  FAIL {rel}  -> {type(exc).__name__}: {exc}")

    if failures:
        print(f'\n{failures} 个组件清单有问题')
        return 1
    print('\n全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
