#!/usr/bin/env python
"""让 LangBot 在 runner 出错时闭嘴——不再往群里发错误提示。

背景
----
LangBot 的 `pkg/pipeline/process/handlers/chat.py` 在捕获
`RunnerNotFoundError` / `RunnerNotAuthorizedError` / `RunnerExecutionError` 时，
**绕过面板里的「输出 → 异常处理」设置**，直接拼一句通知发到聊天里：

    Agent runner temporarily unavailable. Please try again.

结果就是模型一失败（比如 DeepSeek 400），群里就刷一句英文报错。
本脚本把这三个分支的 `user_notice` 改成 `None` —— 错误仍然写日志、仍然出现在
面板「运行记录 / 错误日志」里，只是不再往群里发。

用法
----
    python patch-silent-runner-errors.py            # 打补丁
    python patch-silent-runner-errors.py --dry-run  # 只看会改哪些文件
    python patch-silent-runner-errors.py --revert   # 从 .orig 备份还原

注意
----
补丁打在 uv 的缓存环境里（`...\\uv\\cache\\archive-v0\\<hash>\\...`）。
**升级 LangBot 或清理 uv 缓存后补丁会失效**，重新跑一次本脚本即可。
原始文件会备份成 `chat.py.orig-silent`，删掉即可手动还原。
"""

from __future__ import annotations

import argparse
import glob
import os
import py_compile
import sys

MARKER = '[silent-errors patch]'
BACKUP_SUFFIX = '.orig-silent'

START = '                if isinstance(e, RunnerNotFoundError):'
END = '                else:\n                    # Use existing exception handling'

REPLACEMENT = '''                if isinstance(
                    e,
                    (
                        RunnerNotFoundError,
                        RunnerNotAuthorizedError,
                        RunnerExecutionError,
                    ),
                ):
                    # [silent-errors patch] 不在聊天里广播 runner 错误；
                    # 错误依旧写日志、依旧进面板「运行记录 / 错误日志」。
                    user_notice = None
'''


def candidate_files() -> list[str]:
    """在 uv 缓存里找出所有 LangBot 的 chat.py。"""

    roots = []
    local = os.environ.get('LOCALAPPDATA')
    if local:
        roots.append(os.path.join(local, 'uv', 'cache', 'archive-v0'))
    home = os.path.expanduser('~')
    roots.append(os.path.join(home, 'AppData', 'Local', 'uv', 'cache', 'archive-v0'))
    roots.append(os.path.join(home, '.cache', 'uv', 'archive-v0'))

    pattern = '*/Lib/site-packages/langbot/pkg/pipeline/process/handlers/chat.py'
    posix_pattern = '*/lib/python*/site-packages/langbot/pkg/pipeline/process/handlers/chat.py'

    found: list[str] = []
    for root in dict.fromkeys(roots):
        if not os.path.isdir(root):
            continue
        for pat in (pattern, posix_pattern):
            found.extend(glob.glob(os.path.join(root, pat)))
    return sorted(dict.fromkeys(found))


def patch_one(path: str, dry_run: bool) -> str:
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    if MARKER in text:
        return '已打过补丁'

    start = text.find(START)
    if start == -1:
        return '结构不匹配（版本不同？），跳过'
    end = text.find(END, start)
    if end == -1:
        return '找不到结束标记，跳过'

    new_text = text[:start] + REPLACEMENT + text[end:]

    backup = path + BACKUP_SUFFIX
    if dry_run:
        return '将打补丁'

    if not os.path.exists(backup):
        with open(backup, 'w', encoding='utf-8') as handle:
            handle.write(text)

    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(new_text)

    try:
        py_compile.compile(path, doraise=True)
    except py_compile.PyCompileError as exc:
        # 编译不过就立刻还原，绝不留半截文件
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return f'编译失败已还原：{exc}'

    return '已打补丁'


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
    parser = argparse.ArgumentParser(description='让 LangBot 的 runner 错误不再发到聊天里')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不修改')
    parser.add_argument('--revert', action='store_true', help='从备份还原')
    args = parser.parse_args()

    files = candidate_files()
    if not files:
        print('没找到任何 LangBot 安装，确认先用 uvx / uv tool 跑过一次 langbot')
        return 1

    print(f'找到 {len(files)} 个 LangBot 环境\n')
    touched = 0
    for path in files:
        env = path.split(os.sep)
        env_name = env[env.index('archive-v0') + 1] if 'archive-v0' in env else path
        result = revert_one(path) if args.revert else patch_one(path, args.dry_run)
        if result == '已打补丁':
            touched += 1
        print(f'  {env_name:20s} {result}')

    print()
    if args.dry_run:
        print('（dry-run，未修改任何文件）')
    elif args.revert:
        print('已还原。记得重启 LangBot。')
    else:
        print(f'完成，改动 {touched} 个环境。**重启 LangBot 后生效。**')
        print('升级 LangBot 或清理 uv 缓存后需重新运行本脚本。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
