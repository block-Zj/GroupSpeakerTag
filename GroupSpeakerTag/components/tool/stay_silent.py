"""让模型"选择不说话"变成一个真正的动作，而不是一段文字。

LangBot 侧配套：`patch-silent-reply.py` 会在发送前检查两件事——
1) 本轮是否调用过本工具（看 resp_messages 里的 tool_calls）；
2) 有没有本工具落下的静默标记（兜底，防止 resp_messages 结构变化）。

任一命中，整条回复都不会发出去。
"""

from __future__ import annotations

import os
import tempfile
import time
import typing

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.entities.builtin.provider import session as provider_session

SILENT_DIR_NAME = 'langbot-silent'


def silent_dir() -> str:
    """静默标记目录：用系统临时目录，保证 LangBot 主进程与本插件进程看到同一路径。"""

    return os.path.join(tempfile.gettempdir(), SILENT_DIR_NAME)


def silent_marker_path(query_id: typing.Any) -> str:
    return os.path.join(silent_dir(), f'{str(query_id)}.lock')


def mark_silent(query_id: typing.Any) -> None:
    """落一个标记，等 respback 阶段消费掉（顺便清理一天前的旧标记）。"""

    if query_id is None or str(query_id) == '':
        return

    directory = silent_dir()
    os.makedirs(directory, exist_ok=True)

    with open(silent_marker_path(query_id), 'w', encoding='utf-8') as handle:
        handle.write('silent')

    # 顺手清扫：防止异常情况下标记堆积
    try:
        deadline = time.time() - 86400
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < deadline:
                    os.remove(path)
            except OSError:
                continue
    except Exception:
        pass


class StaySilent(Tool):
    """模型调用它表示"这轮不接话"。"""

    async def call(
        self,
        params: dict[str, typing.Any],
        session: provider_session.Session,
        query_id: int,
    ) -> str:
        reason = str((params or {}).get('reason') or '').strip()

        try:
            mark_silent(query_id)
        except Exception:
            # 落标记失败也不影响主流程：respback 还会看 tool_calls
            pass

        if reason:
            return f'已记录：本轮不回复（原因：{reason}）。现在不要输出任何文字。'
        return '已记录：本轮不回复。现在不要输出任何文字。'
