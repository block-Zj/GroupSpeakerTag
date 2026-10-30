#!/usr/bin/env python
"""让机器人真的"选择不说话"——靠**一次确定的写文件**，不猜文本。

判定顺序（发送前，respback.process）
------------------------------------
1. **标记文件**：插件工具调用时在共享目录落一个 `.marker`，这里扫到即拦截。
   与 query_id、TEMP、消息列表都无关，最可靠。
2. **resp_messages 里的 tool_call**：能扫到就拦，扫不到不影响（已知常常为空）。
3. **显式文本标记**（[[SILENT]] 等）：只剩标记 → 不发；混在正文 → 剥掉后发。

刻意不做的事
------------
- 不做"括号形状"判定（（躺平）（安静）这类）——那是在猜模型的措辞，
  下次它换个写法就漏，属于无底洞。要拦就用工具落标记。

标记目录（按顺序找，命中即止）
------------------------------
1. 环境变量 `LANGBOT_SILENT_MARKER_DIR`
2. `<LangBot 工作目录>/data/silent`（插件默认写这里）
3. `%TEMP%/langbot-silent`（兜底）

用法
----
    python patch-silent-reply.py            # 打补丁 / 从任意旧版重装为 v4
    python patch-silent-reply.py --dry-run
    python patch-silent-reply.py --revert
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[silent-reply patch v4.1]'
BACKUP_SUFFIX = '.orig-silentreply'

ANCHOR = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]
"""

REPLACEMENT = """        response_index = len(query.resp_message_chain) - 1
        message_chain = query.resp_message_chain[-1]

        # [silent-reply patch v4] 标记文件 / 工具调用 / 文本标记 —— 命中即不发
        _marker_hit, _marker_path = _consume_silent_markers()
        if _marker_hit or _silent_tool_requested(query) or _apply_silent_reply_filter(message_chain):
            self.ap.logger.info(
                'Silent reply suppressed for query %s (marker=%s)',
                query.query_id,
                _marker_path or '-',
            )
            return entities.StageProcessResult(
                result_type=entities.ResultType.CONTINUE, new_query=query
            )
"""

HELPER = '''

# ---------------------------------------------------------------------------
# [silent-reply patch v4] 静默回复过滤
# ---------------------------------------------------------------------------

_SILENT_TOOL_NAMES = ('stay_silent', 'silent', 'no_reply')
_SILENT_DEFAULT_MARKERS = ('[[SILENT]]', '[[silent]]', '[[Silence]]', '【SILENT】', '[[静默]]')
_SILENT_MARKER_TTL = 30.0
_SILENT_CACHE = {'mtime': None, 'markers': list(_SILENT_DEFAULT_MARKERS)}


def _silent_marker_dirs() -> list:
    """标记目录候选，按优先级排序去重。"""

    candidates = []
    env_dir = os.environ.get('LANGBOT_SILENT_MARKER_DIR')
    if env_dir and env_dir.strip():
        candidates.append(env_dir.strip())
    candidates.append(os.path.join(os.getcwd(), 'data', 'silent'))
    candidates.append(os.path.join(tempfile.gettempdir(), 'langbot-silent'))

    seen = set()
    ordered = []
    for item in candidates:
        key = os.path.abspath(item)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _consume_silent_markers():
    """扫标记文件：命中返回 (True, 路径)，并把**所有目录**的新鲜标记一起清掉。

    为什么全清：工具为了容错会在多个目录各写一份（data/silent + %TEMP%），
    只删命中那一个的话，剩下那份会在 30 秒内误吞下一条正常回复。
    """

    now = time.time()
    fresh = []
    for directory in _silent_marker_dirs():
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.endswith('.marker'):
                continue
            path = os.path.join(directory, name)
            try:
                age = now - os.path.getmtime(path)
            except OSError:
                continue
            fresh.append((path, age))

    if not fresh:
        return False, ''

    hit = None
    for path, age in fresh:
        if age <= _SILENT_MARKER_TTL and hit is None:
            hit = path
        # 新鲜的、过期的都删掉：一次工具调用只该影响一条回复
        try:
            os.remove(path)
        except OSError:
            pass

    if hit:
        return True, hit
    return False, ''


def _silent_tool_requested(query) -> bool:
    """本轮 resp_messages 里是否有"保持安静"的 tool_call（备用通道）。"""

    for message in getattr(query, 'resp_messages', None) or []:
        for call in getattr(message, 'tool_calls', None) or []:
            function = getattr(call, 'function', None)
            name = ''
            if function is not None:
                name = getattr(function, 'name', '') or ''
                if not name and isinstance(function, dict):
                    name = function.get('name') or ''
            lowered = str(name).strip().lower()
            for wanted in _SILENT_TOOL_NAMES:
                if lowered == wanted or lowered.endswith('/' + wanted) or lowered.endswith('__' + wanted):
                    return True
    return False


def _silent_marker_list():
    """显式标记词：环境变量 > data/silent-reply-markers.txt > 内置默认。"""

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
    """显式文本标记：只剩标记则丢弃；混在正文里则剥掉后发送。"""

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
    if '\nimport time' not in text:
        text = text.replace('import random\n', 'import random\nimport time\n', 1)
    return text


def apply(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        current = handle.read()

    if MARKER in current:
        return '已是 v4'

    # 统一从原始文件重来：有备份先还原，避免旧 helper 叠加
    original = current
    backup = path + BACKUP_SUFFIX
    if os.path.exists(backup):
        with open(backup, encoding='utf-8') as handle:
            original = handle.read()

    if ANCHOR not in original:
        return '结构不匹配（版本不同？），跳过'

    new_text = original.replace(ANCHOR, REPLACEMENT, 1)
    if 'def _consume_silent_markers' not in new_text:
        new_text = new_text.rstrip('\n') + '\n' + HELPER
    new_text = _ensure_imports(new_text)

    if dry_run:
        return '将重装为 v4'

    return _write(path, new_text, original) or '已重装为 v4'


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
    parser = argparse.ArgumentParser(description='模型"不说话"时不发送（v4：标记文件驱动）')
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
        if result.startswith('已重装'):
            touched += 1
        print(f'  {env:22s} {result}')

    print()
    if args.dry_run:
        print('（dry-run，未修改任何文件）')
    elif args.revert:
        print('已还原。记得重启 LangBot。')
    else:
        print(f'完成，改动 {touched} 个环境。**重启 LangBot 后生效。**')
        print('标记目录：<LangBot工作目录>/data/silent ，或环境变量 LANGBOT_SILENT_MARKER_DIR')
    return 0


if __name__ == '__main__':
    sys.exit(main())
