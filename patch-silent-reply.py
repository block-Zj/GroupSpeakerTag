#!/usr/bin/env python
"""让机器人"想安静的时候真的不说话"：模型只输出 [[SILENT]] 时，不发送这条回复。

为什么不能靠插件
----------------
LangBot 的插件事件全是**入站**的（MessageReceived / GroupNormalMessageReceived /
PromptPreProcessing / CommandSent ...），**没有"发送前"事件**。
所以"把这条回复吞掉"只能在发送阶段做，也就是 `pkg/pipeline/respback/respback.py`。

判定规则
--------
1. 取出这条回复里所有 Plain 文本拼起来；
2. 命中标记（默认 `[[SILENT]]`，可配置）后把标记从文本里剥掉；
3. 剥完如果**没有任何文字、也没有图片/文件等其它段** → 整条丢弃，不发送；
   如果剥完还剩下正文（或还有图片）→ 发送清理后的内容。

标记词配置（改完不用重新打补丁，重启生效）
------------------------------------------
优先级：
1. 环境变量 `LANGBOT_SILENT_MARKERS`（逗号分隔）
2. `./data/silent-reply-markers.txt`（一行一个，# 开头为注释）
3. 内置默认：[[SILENT]] / [[silent]] / [[Silence]] / 【SILENT】 / [[静默]]

用法
----
    python patch-silent-reply.py
    python patch-silent-reply.py --dry-run
    python patch-silent-reply.py --revert
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[silent-reply patch]'
BACKUP_SUFFIX = '.orig-silentreply'

ANCHOR = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]
"""

REPLACEMENT = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]

        # [silent-reply patch] 模型只想安静（例如只输出 [[SILENT]]）时不发这条
        if _apply_silent_reply_filter(message_chain):
            self.ap.logger.info(
                'Silent reply suppressed for query %s (marker detected)', query.query_id
            )
            return entities.StageProcessResult(
                result_type=entities.ResultType.CONTINUE, new_query=query
            )
"""

HELPER = '''

# ---------------------------------------------------------------------------
# [silent-reply patch] 静默回复过滤
# ---------------------------------------------------------------------------

_SILENT_DEFAULT_MARKERS = ('[[SILENT]]', '[[silent]]', '[[Silence]]', '【SILENT】', '[[静默]]')
_SILENT_CACHE = {'mtime': None, 'markers': list(_SILENT_DEFAULT_MARKERS)}


def _silent_marker_list():
    """标记词：环境变量 > data/silent-reply-markers.txt > 内置默认。"""

    env = os.environ.get('LANGBOT_SILENT_MARKERS')
    if env and env.strip():
        return [item.strip() for item in env.replace('，', ',').split(',') if item.strip()] or list(
            _SILENT_DEFAULT_MARKERS
        )

    path = os.path.join('data', 'silent-reply-markers.txt')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return list(_SILENT_DEFAULT_MARKERS)

    if _SILENT_CACHE['mtime'] == mtime:
        return _SILENT_CACHE['markers']

    markers = []
    try:
        with open(path, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                markers.append(line)
    except OSError:
        markers = []

    markers = markers or list(_SILENT_DEFAULT_MARKERS)
    _SILENT_CACHE['mtime'] = mtime
    _SILENT_CACHE['markers'] = markers
    return markers


def _apply_silent_reply_filter(chain):
    """命中静默标记就清理文本；整条都空则返回 True（表示丢弃不发送）。"""

    root = getattr(chain, 'root', None)
    if not isinstance(root, list):
        root = list(chain or [])

    plain_parts = [
        item for item in root if isinstance(item, platform_message.Plain) and getattr(item, 'text', '')
    ]
    if not plain_parts:
        return False

    joined = ''.join(item.text or '' for item in plain_parts)
    lowered = joined
    hit = False
    for marker in _silent_marker_list():
        if marker and marker.lower() in lowered.lower():
            hit = True
            break
    if not hit:
        return False

    cleaned = joined
    for marker in _silent_marker_list():
        if not marker:
            continue
        # 大小写不敏感地剥掉标记
        lowered_marker = marker.lower()
        buffer = []
        index = 0
        text = cleaned
        while True:
            pos = text.lower().find(lowered_marker, index)
            if pos < 0:
                buffer.append(text[index:])
                break
            buffer.append(text[index:pos])
            index = pos + len(marker)
        cleaned = ''.join(buffer)
    cleaned = cleaned.strip()

    has_other = any(not isinstance(item, platform_message.Plain) for item in root)
    if not cleaned and not has_other:
        return True

    # 保留第一段文本放清理后的正文，其余文本段清空
    plain_parts[0].text = cleaned
    for item in plain_parts[1:]:
        item.text = ''
    return False
'''


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
            '*/Lib/site-packages/langbot/pkg/pipeline/respback/respback.py',
            '*/lib/python*/site-packages/langbot/pkg/pipeline/respback/respback.py',
        ):
            found.extend(glob.glob(os.path.join(root, pat)))
    return sorted(dict.fromkeys(found))


def apply(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    has_helper = 'def _apply_silent_reply_filter' in text
    if MARKER in text:
        if has_helper:
            return '已打过补丁'
        if dry_run:
            return '将补函数定义'
        new_text = text.rstrip('\n') + '\n' + HELPER
        return _write(path, new_text, text) or '已补函数定义'

    if ANCHOR not in text:
        return '结构不匹配（版本不同？），跳过'

    new_text = text.replace(ANCHOR, REPLACEMENT, 1)
    if not has_helper:
        new_text = new_text.rstrip('\n') + '\n' + HELPER
    if '\nimport os' not in new_text:
        new_text = new_text.replace('import random\n', 'import os\nimport random\n', 1)

    if dry_run:
        return '将打补丁'

    return _write(path, new_text, text) or '已打补丁'


def _write(path: str, new_text: str, original: str) -> str:
    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        with open(backup, 'w', encoding='utf-8') as handle:
            handle.write(original)

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(new_text)

    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(original)
        return f'编译失败已还原：{exc}'
    return ''


def revert(path: str) -> str:
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
    parser = argparse.ArgumentParser(description='模型输出静默标记时不发送回复')
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
        result = revert(path) if args.revert else apply(path, args.dry_run)
        if result in ('已打补丁', '已补函数定义'):
            touched += 1
        print(f'  {env:22s} {result}')

    print()
    if args.dry_run:
        print('（dry-run，未修改任何文件）')
    elif args.revert:
        print('已还原。记得重启 LangBot。')
    else:
        print(f'完成，改动 {touched} 个环境。**重启 LangBot 后生效。**')
        print('标记词可在 data/silent-reply-markers.txt 配置（一行一个），改完重启即可。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
