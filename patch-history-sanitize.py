#!/usr/bin/env python
"""从根上拦住"历史毒消息"：改 Transcript 读取入口 + 图片回退逻辑。

为什么之前的插件消毒层没用
--------------------------
LangBot 4.11 的 `transcript_store.load_history()` 注释写得很直白：

    Runner history is canonical in Transcript. This view exists for
    legacy Pipeline readers such as PromptPreProcessing that still expect
    query.messages.

也就是说：**AgentRunner（LocalAgent）直接从 Transcript 表读历史**，
插件在 `PromptPreProcessing` 里改的 `query.messages` 只是给旧管线看的兼容视图，
改得再漂亮也进不了真正发给模型的请求。

本脚本打两个点：

1. `pkg/agent/runner/transcript_store.py`
   `_transcript_item_to_provider_message()` —— 所有历史消息投影成 Provider 消息的
   必经之路。在这里把 image_url / 空 image_base64 / file_* / 空内容降级成文本占位。
2. `pkg/agent/runner/query_entry_adapter.py`
   `if not contents` 的回退分支里，Image 有 url 无 base64 时原本会生成
   `image_url`（QQ 一次性签名链接）。改成输出 `[图片]` 文本，
   从源头上不再产生新的毒记录。

用法
----
    python patch-history-sanitize.py
    python patch-history-sanitize.py --dry-run
    python patch-history-sanitize.py --revert
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[history-sanitize patch]'
BACKUP_SUFFIX = '.orig-history'

# ---------------------------------------------------------------- 补丁 1：读取入口

TS_ANCHOR = """        content_json = item.get('content_json')
        if isinstance(content_json, dict):
            message_data = dict(content_json)
            message_data['role'] = role
            try:
                return provider_message.Message.model_validate(message_data)
            except Exception:
                pass"""

TS_REPLACEMENT = """        content_json = item.get('content_json')
        if isinstance(content_json, dict):
            message_data = dict(content_json)
            message_data['role'] = role
            # [history-sanitize patch] 历史里的失效图片链接/空元素会让上游直接 400
            message_data['content'] = _sanitize_history_content(message_data.get('content'))
            try:
                return provider_message.Message.model_validate(message_data)
            except Exception:
                pass"""

TS_HELPER = '''

def _sanitize_history_content(content):  # [history-sanitize patch]
    """把历史里送不出去的图片/文件元素降级成文本占位。

    QQ 图片链接带一次性签名 + 来源校验，第三方服务端抓不到会返回 400；
    而 LangBot 落库时又会把 image_base64 抹成 None，重放出来是空元素。
    两者都会让**整段历史**不可用，所以必须在投影成 Provider 消息时清理。
    """

    placeholder_image = '[图片]'
    placeholder_file = '[文件]'
    placeholder_blank = '[空白消息]'

    if isinstance(content, str):
        return content if content.strip() else placeholder_blank
    if not isinstance(content, list):
        return content

    cleaned = []
    for element in content:
        if not isinstance(element, dict):
            cleaned.append(element)
            continue

        etype = element.get('type')
        if etype == 'image_url':
            raw = element.get('image_url')
            url = str((raw or {}).get('url') or '') if isinstance(raw, dict) else ''
            if url.startswith('data:'):
                cleaned.append(element)
            else:
                cleaned.append({'type': 'text', 'text': placeholder_image})
        elif etype == 'image_base64':
            if element.get('image_base64'):
                cleaned.append(element)
            else:
                cleaned.append({'type': 'text', 'text': placeholder_image})
        elif etype in ('file_url', 'file_base64'):
            cleaned.append({'type': 'text', 'text': placeholder_file})
        else:
            cleaned.append(element)

    if not cleaned:
        return [{'type': 'text', 'text': placeholder_blank}]

    if all(
        isinstance(element, dict)
        and element.get('type') == 'text'
        and not str(element.get('text') or '').strip()
        for element in cleaned
    ):
        return [{'type': 'text', 'text': placeholder_blank}]

    return cleaned
'''

# ---------------------------------------------------------------- 补丁 2：来源

QEA_ANCHOR = """                    if image_base64:
                        contents.append({'type': 'image_base64', 'image_base64': image_base64})
                    elif image_url:
                        contents.append({'type': 'image_url', 'image_url': {'url': image_url}})"""

QEA_REPLACEMENT = """                    if image_base64:
                        contents.append({'type': 'image_base64', 'image_base64': image_base64})
                    elif image_url:
                        # [history-sanitize patch] 平台临时链接只能本地消化，绝不交给模型
                        contents.append({'type': 'text', 'text': '[图片]'})"""


def candidate_files(name: str) -> list[str]:
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
            f'*/Lib/site-packages/langbot/pkg/agent/runner/{name}',
            f'*/lib/python*/site-packages/langbot/pkg/agent/runner/{name}',
        ):
            found.extend(glob.glob(os.path.join(root, pat)))
    return sorted(dict.fromkeys(found))


def _write(path: str, text: str, original: str) -> str:
    """写入并编译自检；失败立刻回滚。"""

    backup = path + BACKUP_SUFFIX
    if not os.path.exists(backup):
        with open(backup, 'w', encoding='utf-8') as handle:
            handle.write(original)

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)

    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(original)
        return f'编译失败已还原：{exc}'
    return ''


def apply(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    is_store = path.endswith('transcript_store.py')
    has_helper = 'def _sanitize_history_content' in text

    # 已经打过补丁：只补可能缺失的函数定义（v1 的 bug 会漏掉它）
    if MARKER in text:
        if is_store and not has_helper:
            if dry_run:
                return '将补函数定义'
            error = _write(path, text.rstrip('\n') + '\n' + TS_HELPER, text)
            return error or '已补函数定义（修复漏写的定义）'
        return '已打过补丁'

    if is_store:
        if TS_ANCHOR not in text:
            return '结构不匹配，跳过'
        new_text = text.replace(TS_ANCHOR, TS_REPLACEMENT, 1)
        if not has_helper:
            new_text = new_text.rstrip('\n') + '\n' + TS_HELPER
    elif path.endswith('query_entry_adapter.py'):
        if QEA_ANCHOR not in text:
            return '结构不匹配，跳过'
        new_text = text.replace(QEA_ANCHOR, QEA_REPLACEMENT, 1)
    else:
        return '未知文件，跳过'

    if dry_run:
        return '将打补丁'

    error = _write(path, new_text, text)
    return error or '已打补丁'


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
    parser = argparse.ArgumentParser(description='从 Transcript 读取入口拦住历史毒消息')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--revert', action='store_true')
    args = parser.parse_args()

    total = 0
    for name in ('transcript_store.py', 'query_entry_adapter.py'):
        files = candidate_files(name)
        print(f'\n=== {name}：{len(files)} 个环境 ===')
        for path in files:
            parts = path.split(os.sep)
            env = parts[parts.index('archive-v0') + 1] if 'archive-v0' in parts else path
            result = revert(path) if args.revert else apply(path, args.dry_run)
            if result == '已打补丁':
                total += 1
            print(f'  {env:20s} {result}')

    print()
    if args.dry_run:
        print('（dry-run，未修改任何文件）')
    elif args.revert:
        print('已还原。记得重启 LangBot。')
    else:
        print(f'完成，改动 {total} 处。**重启 LangBot 后生效。**')
    return 0


if __name__ == '__main__':
    sys.exit(main())
