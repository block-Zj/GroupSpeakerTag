"""Group message speaker tag listener (v0.3.0).

问题背景（两件事，一起解决）：

1. 发言人身份
   LangBot 的会话粒度是「群」——同一个群里所有成员共用一份对话历史，
   而发进模型的 user 消息只有正文、没有发送者身份，所以模型分不清谁在说话。
   本插件在 GroupNormalMessageReceived 阶段把「昵称 + ID」拼到用户消息前面
   （通过 user_message_alter），这段文本会同时进入模型上下文和历史。

2. 图片
   LangBot 4.11 的 aiocqhttp 适配器只填 Image.url、**不填 base64**
   （pkg/platform/adapters/aiocqhttp/message_converter.py:164-167），
   而内核 preproc 只认 base64（pkg/pipeline/preproc/preproc.py:350-356），
   base64 为空时图片被静默丢掉。旧实现 qq_image_url_to_base64 已被官方标弃用。
   → 因此本插件在本地把图片下载成 base64 再交给模型。
   QQ 的 multimedia.nt.qq.com.cn 链接带一次性 rkey + 来源校验，
   只有你自己的机器能下，模型服务商的服务器一定下不了（会 400）。

   铁律：**永远不要把平台 URL 交给模型**（ContentElement.from_image_url），
   只能用 ContentElement.from_image_base64。

3. QQ 表情
   4.11 的适配器已经会把 face / rps / dice 解析成 platform_message.Face
   （内置 id→中文名映射，如 14=微笑、182=笑哭），
   但内核 preproc 里没有任何 Face 分支，表情会被静默丢掉。
   → 本插件把 Face 渲染成「[表情]微笑」「[表情]5点的骰子」这类文本；
   纯表情消息因此也不会变成 content 为空的 user 消息。

4. 自检命令（v4 已移除全部内置命令，命令只能由插件提供）
   !whoami —— 报告发言人身份、将要加的前缀、打标与图片统计
   !prompt —— 打印系统提示词 + 最近历史（默认仅管理员/私聊）
"""

from __future__ import annotations

import asyncio
import ast
import base64
import typing
import urllib.request

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events
from langbot_plugin.api.entities.builtin.platform import message as platform_message
from langbot_plugin.api.entities.builtin.provider.message import ContentElement, Message

PLUGIN_VERSION = '0.4.2'

DEFAULT_TAG_FORMAT = '【{name}（QQ:{id}）】'

GUIDE_TEXT = (
    '你在一个多人群聊里，群里有多个不同的成员。'
    '为了让你分清是谁在说话，每条来自群成员的消息开头都会带一个发言人标记，'
    '格式形如 `【昵称（QQ:123456）】`（具体格式由系统配置决定）。'
    '这是系统自动添加的身份标记，不是用户正文的一部分，也不代表用户在对你说话。'
    '请据此判断每句话分别出自谁，不要把不同成员的内容当成同一个人的连续发言；'
    '回答时不要原样复述这个标记。'
)

GUIDE_MARK = '多条群成员消息'

MAX_QUOTE_CHARS = 300
MAX_DUMP_CHARS = 2600
HISTORY_PREVIEW_COUNT = 8

# 图片本地下载
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 10.0

# 历史消息消毒：无法直接送给模型的图片/文件元素降级成这些占位文本
PLACEHOLDER_IMAGE = '[图片]'
PLACEHOLDER_FILE = '[文件]'

DIAGNOSTIC_COMMANDS = ('whoami', 'speaker', 'prompt')

PROMPT_DUMP_VAR = '_gs_prompt_dump'

IMAGE_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)


# --------------------------------------------------------------------------
# 配置与运行时状态
# --------------------------------------------------------------------------

# 配置读取的实际情况：loaded=True 表示某条路径返回了 dict（哪怕是空 dict）
_CONFIG_STATE: dict[str, typing.Any] = {'loaded': False, 'source': '', 'error': ''}


def _plugin_config(component: typing.Any) -> dict[str, typing.Any]:
    """读取插件配置，兼容不同版本的 langbot-plugin SDK。

    - langbot-plugin >= 0.7（langbot 4.11+）：组件上有 get_plugin_config()
    - langbot-plugin 0.5.x（langbot 4.10.x 锁定）：组件上没有，走 plugin.get_config()

    读取结果会记进 _CONFIG_STATE，好让 !whoami 分得清
    「读到了但配置为空」和「彻底没读到」——这两件事以前被混成一句"未取到"。
    """

    _CONFIG_STATE['loaded'] = False
    _CONFIG_STATE['source'] = ''

    getter = getattr(component, 'get_plugin_config', None)
    if callable(getter):
        try:
            value = getter()
            if isinstance(value, dict):
                _CONFIG_STATE.update(loaded=True, source='component.get_plugin_config()', error='')
                return dict(value)
        except Exception as exc:
            _CONFIG_STATE['error'] = f'get_plugin_config: {type(exc).__name__}: {exc}'

    plugin = getattr(component, 'plugin', None)
    if plugin is not None:
        getter = getattr(plugin, 'get_config', None)
        if callable(getter):
            try:
                value = getter()
                if isinstance(value, dict):
                    _CONFIG_STATE.update(loaded=True, source='plugin.get_config()', error='')
                    return dict(value)
            except Exception as exc:
                _CONFIG_STATE['error'] = f'plugin.get_config: {type(exc).__name__}: {exc}'
        value = getattr(plugin, 'config', None)
        if isinstance(value, dict):
            _CONFIG_STATE.update(loaded=True, source='plugin.config', error='')
            return dict(value)

    return {}


# 运行时状态：让"静默失效"也能被看见（!whoami 里会显示）
_STATE: dict[str, typing.Any] = {
    'tag_count': 0,
    'last_tag': '',
    'last_sender': '',
    'last_error': '',
    'images_ok': 0,
    'images_failed': 0,
    'last_image_error': '',
    'sanitized_total': 0,
    'last_sanitize': '',
}


def _note_tag(tag: str, sender: str) -> None:
    _STATE['tag_count'] = int(_STATE.get('tag_count') or 0) + 1
    _STATE['last_tag'] = tag
    _STATE['last_sender'] = sender
    _STATE['last_error'] = ''


def _note_error(exc: BaseException) -> None:
    _STATE['last_error'] = f'{type(exc).__name__}: {exc}'


def _note_image_ok() -> None:
    _STATE['images_ok'] = int(_STATE.get('images_ok') or 0) + 1
    _STATE['last_image_error'] = ''


def _note_image_error(detail: str) -> None:
    _STATE['images_failed'] = int(_STATE.get('images_failed') or 0) + 1
    _STATE['last_image_error'] = detail


def _note_sanitize(count: int, detail: str) -> None:
    _STATE['sanitized_total'] = int(_STATE.get('sanitized_total') or 0) + count
    _STATE['last_sanitize'] = detail


def _get_field(obj: typing.Any, name: str) -> typing.Any:
    """兼容 dict 与 pydantic 模型两种取字段方式。"""

    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _sanitize_content_list(content: typing.Any) -> tuple[typing.Any, int, str]:
    """把无法直接送给模型的图片/文件元素降级成文本占位。

    返回 (新内容, 替换数量, 最近一次替换原因)。

    为什么需要这一步（LangBot 4.11 的连锁反应）：
    1. 历史记录里的 `image_url` 指向 QQ CDN 的一次性签名链接，服务商下载必然失败 → 400；
    2. transcript 落库时会把 `image_base64` 抹成 null，重放出来就是"空图片元素"。
    两者都会污染**整段历史**，一条坏记录能让该会话永远报错，所以必须在请求前清一遍。
    """

    if not isinstance(content, list):
        return content, 0, ''

    out: list[typing.Any] = []
    replaced = 0
    reason = ''

    for element in content:
        etype = str(_get_field(element, 'type') or '')

        if etype == 'image_url':
            raw = _get_field(element, 'image_url')
            url = str(_get_field(raw, 'url') or '') if raw is not None else ''
            if url.startswith('data:'):
                out.append(element)
            else:
                out.append(ContentElement.from_text(PLACEHOLDER_IMAGE))
                replaced += 1
                reason = '历史里的远程图片链接（服务商抓不到）'

        elif etype == 'image_base64':
            if _get_field(element, 'image_base64'):
                out.append(element)
            else:
                out.append(ContentElement.from_text(PLACEHOLDER_IMAGE))
                replaced += 1
                reason = '历史图片载荷为空（落库时 base64 被抹掉）'

        elif etype in ('file_url', 'file_base64'):
            out.append(ContentElement.from_text(PLACEHOLDER_FILE))
            replaced += 1
            reason = '文件元素（OpenAI 兼容接口不支持）'

        else:
            out.append(element)

    return out, replaced, reason


def _sanitize_prompt_images(messages: typing.Any) -> tuple[int, str]:
    """就地清理一组消息里的危险/空图片元素，返回 (替换总数, 原因)。"""

    total = 0
    reason = ''

    for message in messages or []:
        content = _get_field(message, 'content')
        if isinstance(content, str) or content is None:
            continue

        new_content, count, why = _sanitize_content_list(content)
        if not count:
            continue

        total += count
        reason = why or reason

        if isinstance(message, dict):
            message['content'] = new_content
        else:
            try:
                message.content = new_content
            except Exception:
                pass

    return total, reason


def _as_bool(value: typing.Any, default: bool = True) -> bool:
    """配置项容错：面板里可能是 bool，也可能是 'true' / 'false' 字符串。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ('', 'none', 'null'):
        return default
    return text in ('1', 'true', 'yes', 'on', '是', '开')


def _image_mode(value: typing.Any) -> str:
    text = str(value or '').strip().lower()
    if text in ('skip', 'off', 'none', '忽略', '跳过'):
        return 'skip'
    return 'auto'


# --------------------------------------------------------------------------
# 文本 / 内容转换
# --------------------------------------------------------------------------


def _content_to_text(content: typing.Any) -> str:
    """把 Message.content（str 或 ContentElement 列表）拍成纯文本。"""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            item_type = getattr(item, 'type', None)
            if item_type == 'text':
                parts.append(str(getattr(item, 'text', '') or ''))
            elif item_type in ('image_url', 'image_base64'):
                parts.append('[图片]')
            elif item_type in ('file_url', 'file_base64'):
                parts.append('[文件]')
        return ''.join(parts)
    return str(content)


def _plain_text_of(chain: typing.Any) -> str:
    """取出消息链里的纯文本，用于引用消息摘要。"""

    parts: list[str] = []
    for component in chain or []:
        if isinstance(component, platform_message.Plain):
            parts.append(component.text or '')
        elif isinstance(component, platform_message.Image):
            parts.append('[图片]')
        elif isinstance(component, platform_message.Voice):
            parts.append('[语音]')
        elif isinstance(component, platform_message.File):
            parts.append('[文件]')
        elif isinstance(component, platform_message.Face):
            parts.append(_render_face(component))
    return ''.join(parts).strip()


def _render_face(component: typing.Any) -> str:
    """把 QQ 表情渲染成模型能理解的中文文本。

    LangBot 4.11 的适配器已经把 face 段解析成
    Face(face_type, face_id, face_name)（内置 id→中文名映射，
    如 14=微笑、182=笑哭），但内核 preproc 没有 Face 分支，表情会被丢掉。
    """

    face_type = str(getattr(component, 'face_type', 'face') or 'face')
    face_id = getattr(component, 'face_id', 0) or 0
    name = str(getattr(component, 'face_name', '') or '').strip()

    if face_type == 'dice':
        return f'[表情]{face_id}点的{name or "骰子"}'
    if face_type == 'rps':
        hands = {1: '布', 2: '剪刀', 3: '石头'}
        return f'[表情]{name or "猜拳"}({hands.get(face_id, face_id)})'
    if name:
        return f'[表情]{name}'
    return f'[表情:{face_id}]' if face_id else '[表情]'


def _render_unknown(component: typing.Any) -> str:
    """兜底：把未识别的消息段变成简短文本，并避免把长 URL 灌进上下文。

    适配器对未处理的段会生成 Unknown(text='mface:{"summary": "[动画表情]", ...}')。
    """

    text = str(getattr(component, 'text', '') or '').strip()
    if not text:
        return ''

    kind, sep, rest = text.partition(':')
    if sep and rest.startswith('{'):
        try:
            data = ast.literal_eval(rest)
        except Exception:
            data = None
        if isinstance(data, dict):
            summary = str(
                data.get('summary') or data.get('desc') or data.get('prompt') or ''
            ).strip()
            if summary:
                return summary if summary.startswith('[') else f'[{kind}] {summary}'

    if sep and kind and len(kind) <= 12 and kind.isalnum():
        return f'[{kind}]'
    return text[:80]


def _sniff_image_mime(raw: bytes) -> str:
    head = raw[:12]
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith(b'GIF8'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith(b'BM'):
        return 'image/bmp'
    return 'image/jpeg'


async def _fetch_image_data_uri(url: str) -> str | None:
    """在本地把图片下载成 data URI。

    QQ 的 multimedia.nt.qq.com.cn 链接带一次性 rkey 且校验来源，
    只有本机（同一出口 IP + 浏览器 UA）能下载；模型服务商一定抓不到。
    所以必须在本地消化成 base64，绝不能把 URL 转交给模型。
    """

    def _blocking() -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                'User-Agent': IMAGE_BROWSER_UA,
                'Referer': 'https://qun.qq.com/',
                'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            },
        )
        with urllib.request.urlopen(request, timeout=IMAGE_TIMEOUT_SECONDS) as response:
            return response.read(MAX_IMAGE_BYTES + 1)

    try:
        raw = await asyncio.to_thread(_blocking)
    except Exception as exc:
        _note_image_error(f'{type(exc).__name__}: {exc}')
        return None

    if not raw:
        _note_image_error('图片下载为空')
        return None
    if len(raw) > MAX_IMAGE_BYTES:
        _note_image_error(f'图片超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB')
        return None

    mime = _sniff_image_mime(raw)
    encoded = base64.b64encode(raw).decode('ascii')
    return f'data:{mime};base64,{encoded}'


async def _component_to_contents(
    component: typing.Any,
    *,
    image_mode: str,
    tag_quote: bool,
) -> list[ContentElement]:
    """把平台消息段转换成模型可读的 ContentElement。

    注意：图片/文件一律走 base64，绝不使用 from_image_url / from_file_url。
    """

    if isinstance(component, platform_message.Plain):
        text = component.text or ''
        return [ContentElement.from_text(text)] if text else []

    if isinstance(component, platform_message.At):
        display = component.display or str(component.target or '')
        return [ContentElement.from_text(f'@{display}')] if display else []

    if isinstance(component, platform_message.Image):
        if image_mode == 'skip':
            return [ContentElement.from_text('[图片]')]

        existing = getattr(component, 'base64', None)
        if existing:
            return [ContentElement.from_image_base64(existing)]

        url = getattr(component, 'url', None)
        if url:
            data_uri = await _fetch_image_data_uri(url)
            if data_uri:
                _note_image_ok()
                return [ContentElement.from_image_base64(data_uri)]

        return [ContentElement.from_text('[图片]')]

    if isinstance(component, platform_message.Voice):
        if getattr(component, 'base64', None):
            return [ContentElement.from_file_base64(component.base64, 'voice.silk')]
        return [ContentElement.from_text('[语音]')]

    if isinstance(component, platform_message.File):
        name = getattr(component, 'name', None) or 'file'
        if getattr(component, 'base64', None):
            return [ContentElement.from_file_base64(component.base64, name)]
        # 文件可能很大，且平台链接同样第三方不可达 → 只留文字引用
        return [ContentElement.from_text(f'[文件] {name}')]

    if isinstance(component, platform_message.Quote):
        if not tag_quote:
            return []
        quote_text = _plain_text_of(component.origin)
        if not quote_text:
            return []
        if len(quote_text) > MAX_QUOTE_CHARS:
            quote_text = quote_text[:MAX_QUOTE_CHARS] + '…'
        who = str(component.sender_id or '未知')
        return [ContentElement.from_text(f'[引用 {who} 的消息：{quote_text}]')]

    if isinstance(component, platform_message.Face):
        # QQ 系统表情 / 骰子 / 猜拳 —— 内核不处理，这里补上
        return [ContentElement.from_text(_render_face(component))]

    if isinstance(component, platform_message.Unknown):
        rendered = _render_unknown(component)
        return [ContentElement.from_text(rendered)] if rendered else []

    return []


def _sender_identity(event: typing.Any) -> tuple[str, str]:
    """从群消息事件里取发言人的昵称与 ID。"""

    sender = getattr(getattr(event, 'message_event', None), 'sender', None)
    name = str(getattr(sender, 'member_name', '') or '').strip() if sender is not None else ''
    sender_id = str(getattr(sender, 'id', '') or '').strip() if sender is not None else ''

    if not sender_id:
        sender_id = str(getattr(event, 'sender_id', '') or '').strip()
    if not name:
        name = sender_id or '未知成员'

    return name, sender_id or '未知'


async def _read_query_var(event_context: context.EventContext, key: str) -> typing.Any:
    try:
        return await event_context.get_query_var(key)
    except Exception:
        return None


def _render_tag(template: str, name: str, sender_id: str) -> str:
    text = str(template or '').strip() or DEFAULT_TAG_FORMAT
    try:
        return text.format(name=name, id=sender_id)
    except Exception:
        return DEFAULT_TAG_FORMAT.format(name=name or '（未取到）', id=sender_id or '?')


# --------------------------------------------------------------------------
# 自检命令
# --------------------------------------------------------------------------


async def _build_identity_report(event_context: context.EventContext, config: dict) -> str:
    event = event_context.event

    sender_name = str(await _read_query_var(event_context, 'sender_name') or '')
    sender_id = str(await _read_query_var(event_context, 'sender_id') or getattr(event, 'sender_id', '') or '')
    session_id = str(await _read_query_var(event_context, 'session_id') or '')
    group_name = str(await _read_query_var(event_context, 'group_name') or '')
    launcher_type = str(getattr(event, 'launcher_type', '') or '')

    template = str(config.get('tag-format') or '').strip() or DEFAULT_TAG_FORMAT
    tag = _render_tag(template, sender_name or '（未取到）', sender_id or '?')

    lines = [f'【发言人标记 · 自检 v{PLUGIN_VERSION}】']
    if launcher_type == 'group':
        lines.append(f'会话：{session_id or "（未取到）"}　← 同群所有人共用这一份上下文')
        if group_name:
            lines.append(f'群名：{group_name}')
    else:
        lines.append('当前是私聊，插件不对私聊消息加标记。')
    lines.append(f'本次发言人：{sender_name or "（未取到）"}　ID：{sender_id or "?"}')
    lines.append(f'将要加的前缀：{tag}')
    lines.append(f'标签模板：{template}')
    lines.append(f'身份说明注入：{"开" if _as_bool(config.get("inject-guide"), True) else "关"}')
    lines.append(f'图片处理：{_image_mode(config.get("image-mode"))}')
    if _CONFIG_STATE.get('loaded'):
        source = str(_CONFIG_STATE.get('source') or '')
        suffix = '' if config else '；当前配置为空，全部按默认值运行'
        lines.append(f'配置读取：正常（{source}）{suffix}')
    else:
        reason = str(_CONFIG_STATE.get('error') or '')
        lines.append(f'配置读取：未取到，全部按默认值运行{f"（{reason}）" if reason else ""}')
    lines.append(f'已打标消息数：{_STATE.get("tag_count", 0)}')
    if _STATE.get('last_tag'):
        lines.append(f'最近一次实际加的前缀：{_STATE["last_tag"]}（{_STATE.get("last_sender", "")}）')
    lines.append(f'图片：成功 {_STATE.get("images_ok", 0)} 张 / 失败 {_STATE.get("images_failed", 0)} 张')
    sanitized = int(_STATE.get('sanitized_total') or 0)
    if sanitized:
        detail = str(_STATE.get('last_sanitize') or '')
        lines.append(f'历史图片消毒：累计 {sanitized} 处 → [图片]{f"（最近：{detail}）" if detail else ""}')
    else:
        lines.append('历史图片消毒：累计 0 处（历史里没有坏图片元素）')
    if _STATE.get('last_image_error'):
        lines.append(f'⚠ 最近一次图片失败：{_STATE["last_image_error"]}')
    if _STATE.get('last_error'):
        lines.append(f'⚠ 最近一次打标出错：{_STATE["last_error"]}')
    lines.append('')
    lines.append('判断标准：「本次发言人」显示的是你自己的昵称和 ID，就说明身份读取正常。')
    lines.append('换另一个人再发一次，这一行应变成对方的名字。')
    lines.append('这个前缀只出现在送给 AI 的消息里，聊天界面上看不到。')
    return '\n'.join(lines)


def _build_prompt_dump(default_prompt: typing.Any, history_messages: typing.Any) -> str:
    lines = ['【当前提示词快照】', '', '— 系统提示词 —']
    for index, message in enumerate(default_prompt or [], 1):
        role = str(getattr(message, 'role', '') or '')
        text = _content_to_text(getattr(message, 'content', ''))
        lines.append(f'{index}. {role}: {text}')

    lines.append('')
    lines.append(f'— 历史消息（最近 {HISTORY_PREVIEW_COUNT} 条）—')
    recent = list(history_messages or [])[-HISTORY_PREVIEW_COUNT:]
    if not recent:
        lines.append('（空）')
    for message in recent:
        role = str(getattr(message, 'role', '') or '')
        text = _content_to_text(getattr(message, 'content', ''))
        if len(text) > 300:
            text = text[:300] + '…'
        lines.append(f'- {role}: {text}')

    dump = '\n'.join(lines)
    if len(dump) > MAX_DUMP_CHARS:
        dump = dump[:MAX_DUMP_CHARS] + '\n…[内容过长已截断]'
    return dump


async def _maybe_handle_diagnostic(component: typing.Any, event_context: context.EventContext) -> None:
    """接管 !whoami / !prompt。命中就自己回复并阻断默认行为。"""

    event = event_context.event
    command = str(getattr(event, 'command', '') or '').strip().lower()
    if command not in DIAGNOSTIC_COMMANDS:
        return

    config = _plugin_config(component)

    if command in ('whoami', 'speaker'):
        text = await _build_identity_report(event_context, config)
    else:
        scope = str(config.get('prompt-dump-scope') or 'admin').strip().lower()
        is_person = str(getattr(event, 'launcher_type', '') or '') == 'person'
        if scope.startswith('off') or scope.startswith('关闭'):
            text = '!prompt 快照已在插件配置里关闭（prompt-dump-scope = off）。'
        elif (
            scope.startswith('everyone')
            or scope.startswith('所有')
            or is_person
            or bool(getattr(event, 'is_admin', False))
        ):
            dump = await _read_query_var(event_context, PROMPT_DUMP_VAR)
            text = str(dump) if dump else '（没抓到快照，请再发一次 !prompt）'
        else:
            text = (
                '!prompt 在群里默认仅限机器人管理员使用（私聊机器人可直接查看）。\n'
                '开法一：面板 → 机器人 → 管理员，把你自己（或本群）加进去；\n'
                '开法二：插件配置里把 prompt-dump-scope 改成 everyone（会向群里暴露系统提示词，谨慎）。'
            )

    try:
        await event_context.reply(platform_message.MessageChain([platform_message.Plain(text=text)]))
    except Exception:
        # 回复失败就交回给流水线，让用户至少能看到默认提示
        return

    event_context.prevent_default()


# --------------------------------------------------------------------------
# 事件监听
# --------------------------------------------------------------------------


class GroupSpeakerTagEventListener(EventListener):
    async def initialize(self) -> None:
        await super().initialize()

        @self.handler(events.GroupNormalMessageReceived)
        async def on_group_normal_message(event_context: context.EventContext) -> None:
            try:
                config = _plugin_config(self)
                event = event_context.event

                name, sender_id = _sender_identity(event)
                template = str(config.get('tag-format') or '').strip() or DEFAULT_TAG_FORMAT
                tag = _render_tag(template, name, sender_id)

                tag_quote = _as_bool(config.get('tag-quote'), True)
                mode = _image_mode(config.get('image-mode'))

                components = list(event.message_chain or [])
                if not components:
                    return

                # 多个图片并行下载，避免串行等待
                results = await asyncio.gather(
                    *(_component_to_contents(item, image_mode=mode, tag_quote=tag_quote) for item in components),
                    return_exceptions=True,
                )

                contents: list[ContentElement] = []
                for item in results:
                    if isinstance(item, list):
                        contents.extend(item)

                if not contents:
                    # 空消息或纯 @，不做改写
                    return

                event.user_message_alter = [ContentElement.from_text(tag)] + contents
                _note_tag(tag, f'{name}({sender_id})')
            except Exception as exc:
                # 插件绝不能阻断主流程：出错就保持原消息不动，但把错误记下来给 !whoami 看
                _note_error(exc)
                return

        @self.handler(events.PromptPreProcessing)
        async def on_prompt_pre_processing(event_context: context.EventContext) -> None:
            try:
                config = _plugin_config(self)
                event = event_context.event
                session_name = str(getattr(event, 'session_name', '') or '')
                is_group = session_name.startswith('group_')

                if _as_bool(config.get('inject-guide'), True) and is_group:
                    default_prompt = getattr(event, 'default_prompt', None)
                    if default_prompt is not None:
                        already = False
                        for message in default_prompt:
                            content = getattr(message, 'content', None)
                            if isinstance(content, str) and GUIDE_MARK in content:
                                already = True
                                break
                        if not already:
                            default_prompt.append(Message(role='system', content=GUIDE_TEXT))

                # 先消毒，再生成快照 —— 快照里看到的才是真正送给模型的内容
                cleaned = 0
                clean_reason = ''
                for attr in ('default_prompt', 'prompt'):
                    count, why = _sanitize_prompt_images(getattr(event, attr, None))
                    if count:
                        cleaned += count
                        clean_reason = why or clean_reason
                if cleaned:
                    _note_sanitize(cleaned, clean_reason)

                dump = _build_prompt_dump(
                    getattr(event, 'default_prompt', None),
                    getattr(event, 'prompt', None),
                )
                await event_context.set_query_var(PROMPT_DUMP_VAR, dump)
            except Exception:
                return

        @self.handler(events.GroupCommandSent)
        async def on_group_command(event_context: context.EventContext) -> None:
            try:
                await _maybe_handle_diagnostic(self, event_context)
            except Exception as exc:
                _note_error(exc)
                return

        @self.handler(events.PersonCommandSent)
        async def on_person_command(event_context: context.EventContext) -> None:
            try:
                await _maybe_handle_diagnostic(self, event_context)
            except Exception as exc:
                _note_error(exc)
                return
