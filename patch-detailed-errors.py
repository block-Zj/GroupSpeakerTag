#!/usr/bin/env python
"""让 LangBot 保留上游模型的原始报错，别再只剩一句"参数错误"。

背景
----
`pkg/provider/modelmgr/requesters/litellmchat.py` 的错误映射表里：

    (litellm.BadRequestError, 'bad_request', t('Invalid request parameters'), False)

任何 400 都会被压成这一句，**上游原文（DeepSeek 返回的 error.message）被直接丢弃**。
于是面板上永远只有 `Model request failed: Invalid request parameters`，
看不出是图片下不动、消息为空、还是工具 schema 不合规。

本脚本在读 `body` 的那段旁边补一行，把 `error.message` 拼回 message 后面。
错误只进日志和面板，不会发到群里（配合 patch-silent-runner-errors.py 更彻底）。

用法
----
    python patch-detailed-errors.py
    python patch-detailed-errors.py --dry-run
    python patch-detailed-errors.py --revert
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[detail-errors patch v2]'
MARKER_V1 = '[detail-errors patch]'
BACKUP_SUFFIX = '.orig-detail'

ANCHOR = """                if upstream_code in ('insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached'):
                    code, retryable = 'quota_exceeded', False
                raise errors.RequesterError(message, code=code, retryable=retryable, **metadata) from e"""

REPLACEMENT_V1 = """                if upstream_code in ('insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached'):
                    code, retryable = 'quota_exceeded', False
                # [detail-errors patch] 保留上游原始报错，否则 400 永远只剩一句"参数错误"
                if isinstance(upstream_error, dict):
                    _upstream_detail = str(upstream_error.get('message') or '').strip()
                    if _upstream_detail and _upstream_detail not in message:
                        message = f'{message}: {_upstream_detail}'
                raise errors.RequesterError(message, code=code, retryable=retryable, **metadata) from e"""

# v2：body 不是 dict 时（litellm 常把原始报文塞在异常字符串里）退而取 str(e)
REPLACEMENT = """                if upstream_code in ('insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached'):
                    code, retryable = 'quota_exceeded', False
                # [detail-errors patch v2] 保留上游原始报错，否则 400 永远只剩一句"参数错误"
                _detail = ''
                if isinstance(upstream_error, dict):
                    _detail = str(upstream_error.get('message') or '').strip()
                if not _detail:
                    _detail = str(e).strip()[:400]
                if _detail and _detail not in message:
                    message = f'{message}: {_detail}'
                raise errors.RequesterError(message, code=code, retryable=retryable, **metadata) from e"""


def candidate_files() -> list[str]:
    roots = []
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(os.path.join(local, 'uv', 'cache', 'archive-v0'))
    home = os.path.expanduser('~')
    roots.extend([
        os.path.join(home, 'AppData', 'Local', 'uv', 'cache', 'archive-v0'),
        os.path.join(home, '.cache', 'uv', 'archive-v0'),
    ])

    found: list[str] = []
    for root in dict.fromkeys(roots):
        if not os.path.isdir(root):
            continue
        for pat in (
            '*/Lib/site-packages/langbot/pkg/provider/modelmgr/requesters/litellmchat.py',
            '*/lib/python*/site-packages/langbot/pkg/provider/modelmgr/requesters/litellmchat.py',
        ):
            found.extend(glob.glob(os.path.join(root, pat)))
    return sorted(dict.fromkeys(found))


def patch_one(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    if MARKER in text:
        return '已是 v2'
    if MARKER_V1 in text:
        new_text = text.replace(REPLACEMENT_V1, REPLACEMENT, 1)
        if new_text == text:
            return 'v1 补丁块与预期不符，请手动处理'
        action = '已升级到 v2'
    elif ANCHOR in text:
        new_text = text.replace(ANCHOR, REPLACEMENT, 1)
        action = '已打补丁'
    else:
        return '结构不匹配（版本不同？），跳过'

    if dry_run:
        return '将打补丁' if action == '已打补丁' else '将升级到 v2'

    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        with open(backup, 'w', encoding='utf-8') as handle:
            handle.write(text)

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(new_text)

    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return f'编译失败已还原：{exc}'
    return action


def revert_one(path: str) -> str:
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        return '没有备份，跳过'
    with open(backup, encoding='utf-8') as handle:
        original = handle.read()
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(original)
    py_compile.compile(path, doraise=False)
    return '已还原'


def main() -> int:
    parser = argparse.ArgumentParser(description='让 LangBot 保留上游模型原始报错')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--revert', action='store_true')
    args = parser.parse_args()

    files = candidate_files()
    if not files:
        print('没找到任何 LangBot 安装')
        return 1

    print(f'找到 {len(files)} 个 LangBot 环境\n')
    touched = 0
    for path in files:
        parts = path.split(os.sep)
        env = parts[parts.index('archive-v0') + 1] if 'archive-v0' in parts else path
        result = revert_one(path) if args.revert else patch_one(path, args.dry_run)
        if result == '已打补丁':
            touched += 1
        print(f'  {env:20s} {result}')

    print()
    if args.dry_run:
        print('（dry-run，未修改任何文件）')
    elif args.revert:
        print('已还原。记得重启 LangBot。')
    else:
        print(f'完成，改动 {touched} 个环境。**重启 LangBot 后生效。**')
    return 0


if __name__ == '__main__':
    sys.exit(main())
