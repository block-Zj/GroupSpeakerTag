"""让模型"选择不说话"变成一个真正的动作，而不是一段文字。

工作方式
--------
被调用时在**共享目录**落一个 `.marker` 文件（默认 `<LangBot数据目录>/data/silent`），
LangBot 发送回复前会扫这个目录：30 秒内有新标记 → 这条回复不发，标记被消费掉。

关键点：标记不依赖 query_id、不依赖 TEMP、不依赖模型输出什么文字。
只要工具被调用过，就一定留下痕迹。

诊断
----
每次调用都会往 `<标记目录>/_tool.log` 追加一行（时间、query_id、会话、写入结果），
排查时直接看这个文件即可。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
import typing

from langbot_plugin.api.definition.components.tool.tool import Tool
from langbot_plugin.api.entities.builtin.provider import session as provider_session

# 与 patch-silent-reply.py 的默认目录保持一致（都指向 LangBot 的 data/silent）
DEFAULT_MARKER_DIR = r'D:\Desktop\Ling\langbot\data\silent'


def _candidate_dirs(configured: str) -> list[str]:
    """写哪些目录：配置目录（主）+ 系统临时目录（兜底）。"""

    candidates = []
    if configured and configured.strip():
        candidates.append(configured.strip())
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


def _session_key(session: typing.Any) -> str:
    """尽量拿到 group_<群号> / person_<QQ> 这种键，拿不到就 unknown。"""

    if session is None:
        return 'unknown'

    launcher_type = getattr(session, 'launcher_type', None)
    type_value = getattr(launcher_type, 'value', launcher_type) or ''
    launcher_id = getattr(session, 'launcher_id', '') or ''

    key = f'{type_value}_{launcher_id}'.strip('_')
    return key or 'unknown'


def _read_config(component: typing.Any) -> dict:
    getter = getattr(component, 'get_plugin_config', None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    return {}


def _write_markers(dirs: list[str], session_key: str, query_id: typing.Any, reason: str) -> tuple[list[str], list[str]]:
    """逐个目录写标记，返回 (写成的路径, 出错信息)。"""

    written: list[str] = []
    errors: list[str] = []

    stamp = int(time.time() * 1000)
    payload = {
        'session': session_key,
        'query_id': query_id if query_id is not None else None,
        'reason': reason,
        'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
    }

    for directory in dirs:
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f'{session_key}-{stamp}.marker')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False)
            written.append(path)
        except Exception as exc:
            errors.append(f'{directory}: {type(exc).__name__}: {exc}')

    return written, errors


def _append_log(directory: str, line: str) -> None:
    try:
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, '_tool.log'), 'a', encoding='utf-8') as handle:
            handle.write(f'[{datetime.datetime.now().strftime("%m-%d %H:%M:%S")}] {line}\n')
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
        config = _read_config(self)
        configured = str(config.get('silent-marker-dir') or '').strip() or DEFAULT_MARKER_DIR

        session_key = _session_key(session)
        dirs = _candidate_dirs(configured)
        written, errors = _write_markers(dirs, session_key, query_id, reason)

        _append_log(
            configured,
            f'session={session_key} query_id={query_id!r} reason={reason or "-"} '
            f'written={len(written)} errors={errors or "-"} paths={written}',
        )

        if written:
            return f'已记录：本轮不回复（标记 {len(written)} 处）。系统会据此不发送任何消息。'
        return f'已记录：本轮不回复。但标记写入失败（{errors[:1]}），请直接输出 [[SILENT]]。'
