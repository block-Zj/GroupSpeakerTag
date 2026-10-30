#!/usr/bin/env python
"""让机器人真的"选择不说话"：模型调用 stay_silent 工具（或只输出静默标记）时，不发送这条回复。

为什么要做两步
--------------
1. **给模型一个工具**（插件里的 `components/tool/stay_silent.yaml`）：
   模型想不说话时应该"调用工具"，而不是吐一段 `[[SILENT]]` 文字占位符——
   文字对系统没有意义，发到群里就是四个字符。
2. **在发送阶段拦掉**（本补丁）：LangBot 的插件事件全是入站的，没有"发送前"事件，
   所以只能在 `pkg/pipeline/respback/respback.py` 里判断并丢弃。

判定顺序
--------
a. 本轮 `resp_messages` 里出现过 tool_call，名字属于 `stay_silent / silent / no_reply`
   → 整条丢弃；
b. 兜底：工具写了静默标记（`%TEMP%/langbot-silent/<query_id>.lock`）→ 消费并丢弃；
c. 都没有：若回复文本只有 `[[SILENT]]` 这类标记 → 丢弃；若标记混在正文里 → 剥掉标记后发送。

用法
----
    python patch-silent-reply.py            # 打补丁 / 从 v1 升级
    python patch-silent-reply.py --dry-run
    python patch-silent-reply.py --revert
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[silent-reply patch v2]'
MARKER_V1 = '[silent-reply patch]'
BACKUP_SUFFIX = '.orig-silentreply'

ANCHOR = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]
"""

REPLACEMENT = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]

        # [silent-reply patch v2] 模型调用 stay_silent 工具、或只输出静默标记时，不发这条
        if _silent_tool_requested(query) or _apply_silent_reply_filter(message_chain):
            self.ap.logger.info('Silent reply suppressed for query %s', query.query_id)
            return entities.StageProcessResult(
                result_type=entities.ResultType.CONTINUE, new_query=query
            )
"""

V1_BLOCK = """        # [silent-reply patch] 模型只想安静（例如只输出 [[SILENT]]）时不发这条
        if _apply_silent_reply_filter(message_chain):
            self.ap.logger.info(
                'Silent reply suppressed for query %s (marker detected)', query.query_id
            )
"""

V1_BLOCK_REPLACEMENT = """        # [silent-reply patch v2] 模型调用 stay_silent 工具、或只输出静默标记时，不发这条
        if _silent_tool_requested(query) or _apply_silent_reply_filter(message_chain):
            self.ap.logger.info('Silent reply suppressed for query %s', query.query_id)
"""

HELPER = '''

# ---------------------------------------------------------------------------
# [silent-reply patch v2] 静默回复过滤
# ---------------------------------------------------------------------------

_SILENT_TOOL_NAMES = ('stay_silent', 'silent', 'no_reply')
_SILENT_DEFAULT_MARKERS = ('[[SILENT]]', '[[silent]]', '[[Silence]]', '【SILENT】', '[[静默]]')
_SILENT_CACHE = {'mtime': None, 'markers': list(_SILENT_DEFAULT_MARKERS)}


def _silent_marker_dir():
    return os.path.join(tempfile.gettempdir(), 'langbot-silent')


def _silent_tool_requested(query) -> bool:
    """本轮是否调用了"保持安静"工具；顺带消费工具落下的标记。"""

    for message in getattr(query, 'resp_messages', None) or []:
        for call in getattr(message, 'tool_calls', None) or []:
            function = getattr(call, 'function', None)
            name = ''
            if function is not None:
                name = getattr(function, 'name', '') or ''
                if not name and isinstance(function, dict):
                    name = function.get('name') or ''
            if str(name).strip().lower() in _SILENT_TOOL_NAMES:
                return True

    marker = os.path.join(_silent_marker_dir(), f'{getattr(query, "query_id", "")}.lock')
    if os.path.exists(marker):
        try:
            os.remove(marker)
        except OSError:
            pass
        return True

    return False


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


def _apply_silent_reply_filter(chain) -> bool:
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
    if not any(marker and marker.lower() in joined.lower() for marker in _silent_marker_list()):
        return False

    cleaned = joined
    for marker in _silent_marker_list():
        if not marker:
            continue
        lowered_marker = marker.lower()
        buffer = []
        index = 0
        text = cleaned
        while True:
            position = text.lower().find(lowered_marker, index)
            if position < 0:
                buffer.append(text[index:])
                break
            buffer.append(text[index:position])
            index = position + len(marker)
        cleaned = ''.join(buffer)
    cleaned = cleaned.strip()

    has_other = any(not isinstance(item, platform_message.Plain) for item in root)
    if not cleaned and not has_other:
        return True

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


def _ensure_imports(text: str) -> str:
    if '\nimport os' not in text:
        text = text.replace('import random\n', 'import os\nimport random\n', 1)
    if '\nimport tempfile' not in text:
        text = text.replace('import random\n', 'import random\nimport tempfile\n', 1)
    return text


def apply(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    has_tool_helper = 'def _silent_tool_requested' in text

    if MARKER in text:
        if has_tool_helper:
            return '已打过补丁 v2'
        if dry_run:
            return '将补齐 v2 函数'
        new_text = _ensure_imports(text.rstrip('\n') + '\n' + HELPER)
        return _write(path, new_text, text) or '已补齐 v2 函数'

    # 从 v1 升级
    if MARKER_V1 in text:
        new_text = text.replace(V1_BLOCK, V1_BLOCK_REPLACEMENT, 1)
        if 'def _silent_tool_requested' not in new_text:
            new_text = new_text.rstrip('\n') + '\n' + HELPER
        new_text = _ensure_imports(new_text)
        if dry_run:
            return '将从 v1 升级到 v2'
        return _write(path, new_text, text) or '已升级到 v2'

    if ANCHOR not in text:
        return '结构不匹配（版本不同？），跳过'

    new_text = text.replace(ANCHOR, REPLACEMENT, 1)
    if 'def _silent_tool_requested' not in new_text:
        new_text = new_text.rstrip('\n') + '\n' + HELPER
    new_text = _ensure_imports(new_text)

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
    parser = argparse.ArgumentParser(description='模型调用 stay_silent 时不发送回复')
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
        if result in ('已打补丁', '已升级到 v2', '已补齐 v2 函数'):
            touched += 1
        print(f'  {env:22s} {result}')

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
