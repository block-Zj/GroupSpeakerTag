"""GroupSpeakerTag v0.3.0 离线自检。

不连 QQ、不连 LangBot，直接用 langbot_plugin SDK 驱动插件模块。
重点验证：图片必须在本地转成 base64，绝不能把平台 URL 交给模型。

用法：
    pip install "langbot-plugin==0.7.11"     # LangBot 4.11.x
    python GroupSpeakerTag-离线自检.py
    pip install "langbot-plugin==0.5.8"      # LangBot 4.10.x，再跑一次
"""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata as md
import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "GroupSpeakerTag"
MOD = ROOT / "components" / "event_listener" / "speaker_tag.py"

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SDK_VERSION = md.version("langbot-plugin")
print(f"=== langbot-plugin {SDK_VERSION} ===")

# ---------------------------------------------------------------- 加载模块
spec = importlib.util.spec_from_file_location("speaker_tag", MOD)
st = importlib.util.module_from_spec(spec)
sys.modules["speaker_tag"] = st
spec.loader.exec_module(st)

from langbot_plugin.api.definition.components.base import BaseComponent
from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.definition.plugin import BasePlugin
from langbot_plugin.api.entities import events
from langbot_plugin.api.entities.builtin.platform import message as platform_message
from langbot_plugin.api.entities.builtin.provider.message import ContentElement, Message

print("[1] 模块导入成功")
print(f"    BaseComponent.get_plugin_config 存在? {hasattr(BaseComponent, 'get_plugin_config')}")


# ---------------------------------------------------------------- 配置读取
class OldStyle(BaseComponent):
    def __init__(self, cfg):
        super().__init__()
        self.plugin = BasePlugin.__new__(BasePlugin)
        self.plugin.config = cfg


class NewStyle(BaseComponent):
    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg

    def get_plugin_config(self):
        return self._cfg


class Broken:
    pass


cfg = {"tag-format": "【{name}】", "image-mode": "auto"}
assert st._plugin_config(OldStyle(cfg)) == cfg, "旧 SDK 路径失败"
assert st._plugin_config(NewStyle(cfg)) == cfg, "新 SDK 路径失败"
assert st._plugin_config(Broken()) == {}, "兜底路径失败"
assert st._CONFIG_STATE.get("loaded") is False, "无配置源时应记为未取到"
# 空配置也是"读取成功"，不能报成"未取到"（v0.4.2 修的就是这个误报）
assert st._plugin_config(NewStyle({})) == {}
assert st._CONFIG_STATE.get("loaded") is True, "空配置应记为读取成功"
assert "get_plugin_config" in str(st._CONFIG_STATE.get("source")), st._CONFIG_STATE.get("source")
print("[2] 配置读取三路径通过（空配置也识别为读取成功）")


# ---------------------------------------------------------------- 本地图床
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if "broken" in self.path:
            self.send_error(403)
            return
        body = PNG_BYTES
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
# 模拟 QQ 的链接形状：带 appid/fileid/rkey 查询串
IMG_URL = f"http://127.0.0.1:{port}/download?appid=1407&fileid=EhRIf2GR&rkey=CAMSMGQ3Max"


class FakePlugin(BasePlugin):
    def __init__(self, config):
        self.config = config

    async def initialize(self):
        pass


async def main():
    # ------------------------------------------------------------ 事件注册
    listener = st.GroupSpeakerTagEventListener()
    listener.plugin = FakePlugin(cfg)
    await listener.initialize()
    registered = {k.__name__: len(v) for k, v in listener.registered_handlers.items()}
    for name in ("GroupMessageReceived", "GroupNormalMessageReceived", "NormalMessageResponded", "PromptPreProcessing", "GroupCommandSent", "PersonCommandSent"):
        assert name in registered, f"缺少事件 {name}"
    print(f"[3] 事件注册通过: {registered}")

    # ------------------------------------------------------------ 纯文本
    plain = platform_message.Plain(text="今天下午三点开会")
    out = await st._component_to_contents(plain, image_mode="auto", tag_quote=True)
    assert len(out) == 1 and out[0].type == "text" and out[0].text == "今天下午三点开会"
    print(f"[4] 文本转换通过: {out[0].text!r}")

    # ------------------------------------------------------------ 图片：本地下载转 base64
    image = platform_message.Image(url=IMG_URL)
    assert not getattr(image, "base64", None), "构造的图片不该带 base64"
    out = await st._component_to_contents(image, image_mode="auto", tag_quote=True)

    types = [e.type for e in out]
    assert "image_url" not in types, f"出现了 image_url，会把平台链接泄给模型: {types}"
    assert types == ["image_base64"], f"期望 image_base64，实际 {types}"

    data_uri = out[0].image_base64
    assert data_uri.startswith("data:image/png;base64,"), f"MIME 识别错误: {data_uri[:40]}"
    decoded = base64.b64decode(data_uri.split(",", 1)[1])
    assert decoded == PNG_BYTES, "base64 内容与原图不一致"
    assert st._STATE["images_ok"] == 1
    print(f"[5] 图片本地下载转 base64 通过: {data_uri[:38]}... ({len(decoded)} 字节)")

    # ------------------------------------------------------------ 图片：下载失败降级
    bad = platform_message.Image(url=f"http://127.0.0.1:{port}/broken?appid=1407")
    out = await st._component_to_contents(bad, image_mode="auto", tag_quote=True)
    assert [e.type for e in out] == ["text"] and out[0].text == "[图片]", f"降级失败: {out}"
    assert st._STATE["images_failed"] == 1
    print(f"[6] 下载失败降级为「[图片]」通过，last_image_error={st._STATE['last_image_error'][:60]!r}")

    # ------------------------------------------------------------ 图片：skip 模式
    out = await st._component_to_contents(image, image_mode="skip", tag_quote=True)
    assert [e.type for e in out] == ["text"] and out[0].text == "[图片]"
    print("[7] image-mode=skip 直接占位通过")

    # ------------------------------------------------------------ QQ 表情
    face = platform_message.Face(face_id=14, face_name="微笑")
    out = await st._component_to_contents(face, image_mode="auto", tag_quote=True)
    assert [e.type for e in out] == ["text"] and out[0].text == "[表情]微笑", out
    assert len(out) > 0, "纯表情消息不能变成空内容"

    dice = platform_message.Face(face_type="dice", face_id=5, face_name="骰子")
    out = await st._component_to_contents(dice, image_mode="auto", tag_quote=True)
    assert out[0].text == "[表情]5点的骰子", out[0].text

    rps = platform_message.Face(face_type="rps", face_id=2, face_name="猜拳")
    out = await st._component_to_contents(rps, image_mode="auto", tag_quote=True)
    assert out[0].text == "[表情]猜拳(剪刀)", out[0].text

    no_name = platform_message.Face(face_id=178, face_name="")
    out = await st._component_to_contents(no_name, image_mode="auto", tag_quote=True)
    assert out[0].text == "[表情:178]", out[0].text

    print("[8] QQ 表情渲染通过: [表情]微笑 / [表情]5点的骰子 / [表情]猜拳(剪刀) / [表情:178]")

    # ------------------------------------------------------------ 表情包（mface → Unknown）
    unknown = platform_message.Unknown(text="mface:{'summary': '[动画表情]', 'url': 'https://multimedia.nt.qq.com.cn/x'}")
    out = await st._component_to_contents(unknown, image_mode="auto", tag_quote=True)
    assert out[0].text == "[动画表情]", out[0].text
    assert "https://" not in out[0].text, "未识别段不能把 URL 灌进上下文"
    print("[9] 表情包(mface)兜底通过: [动画表情]（URL 已剔除）")

    # ------------------------------------------------------------ 整条消息拼装
    st._STATE.update({"tag_count": 0, "last_tag": "", "last_error": ""})
    tag = st._render_tag("【{name}（QQ:{id}）】", "张三", "10001")
    assert tag == "【张三（QQ:10001）】"
    st._note_tag(tag, "张三(10001)")
    assert st._STATE["tag_count"] == 1
    print(f"[10] 标记渲染通过: {tag}")

    # ------------------------------------------------------------ 历史图片消毒
    history = [
        Message(role="user", content=[
            ContentElement.from_text("【张三（QQ:10001）】"),
            ContentElement.from_image_url("https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=x"),
        ]),
        Message(role="user", content=[
            ContentElement.from_text("【李四（QQ:10002）】"),
            ContentElement(type="image_base64", image_base64=None, content_redacted=True),
        ]),
        Message(role="user", content=[ContentElement.from_text("这条是纯文字，不该被动")]),
    ]
    st._STATE.update({"sanitized_total": 0, "last_sanitize": ""})
    cleaned, why = st._sanitize_prompt_images(history)
    assert cleaned == 2, f"应清洗 2 处，实际 {cleaned}"
    assert history[0].content[1].type == "text" and history[0].content[1].text == "[图片]"
    assert history[1].content[1].type == "text" and history[1].content[1].text == "[图片]"
    assert history[2].content[0].text == "这条是纯文字，不该被动"
    for msg in history:
        for element in msg.content:
            assert element.type != "image_url", "消毒后绝不能残留 image_url"
            if element.type == "image_base64":
                raise AssertionError("消毒后不该有空的 image_base64")
    # data URI 是合法的，必须原样保留
    keep = [Message(role="user", content=[
        ContentElement.from_image_base64("data:image/png;base64,iVBORw0KGgo="),
    ])]
    kept, _ = st._sanitize_prompt_images(keep)
    assert kept == 0 and keep[0].content[0].type == "image_base64", "data URI 不该被清洗"
    print(f"[11] 历史图片消毒通过: {cleaned} 处 → [图片]（{why}），data URI 原样保留")

    # ------------------------------------------------------------ 空消息补齐
    blanks = [
        Message(role="user", content=[]),
        Message(role="user", content=""),
        Message(role="user", content="   "),
        Message(role="user", content=[ContentElement.from_text(" ") ]),
        Message(role="assistant", content="保留我"),
    ]
    st._STATE.update({"blank_fixed": 0})
    fixed = st._sanitize_blank_messages(blanks)
    assert fixed == 4, f"应补齐 4 条，实际 {fixed}"
    for msg in blanks[:4]:
        assert msg.content[0].text == "[空白消息]", msg.content
    assert blanks[4].content == "保留我", "正常消息不该被动"
    # 已经补过的不要重复计数
    again = st._sanitize_blank_messages([Message(role="user", content="[空白消息]")])
    assert again == 0, "占位文本不应被再次处理"
    print(f"[12] 空消息补齐通过: {fixed} 处 → [空白消息]，且幂等")


    # ------------------------------------------------------------ 静默开关判定
    mute_triggers = st._split_triggers("灵灵安静, 安静灵灵", "灵灵安静")
    wake_triggers = st._split_triggers("灵灵", "灵灵")
    assert mute_triggers == ["灵灵安静", "安静灵灵"], mute_triggers

    cases = [
        ("灵灵安静", False, False, "mute"),      # 正常状态下收到静默词
        ("灵灵安静", False, True, "mute"),       # 已静默时再收到静默词
        ("灵灵安静一点", False, False, "mute"),  # 包含即可
        ("今天天气不错", False, True, "block"),  # 静默期间普通消息 -> 拦截
        ("灵灵", False, True, "unmute"),         # 唤醒词
        ("灵灵在吗", False, True, "unmute"),     # 含唤醒词
        ("随便说说", True, True, "unmute"),      # @ 她 -> 唤醒
        ("灵灵", False, False, "pass"),          # 未静默时正常放行
        ("随便说说", True, False, "pass"),       # 未静默时 @ 不影响
    ]
    for text, at_me, muted, expect in cases:
        got = st._quiet_action(text, at_me, muted, mute_triggers, wake_triggers)
        assert got == expect, f"{text!r} at={at_me} muted={muted}: 期望 {expect}，实际 {got}"

    print(f"[13] 静默开关判定通过: {len(cases)} 个用例（静默/拦截/唤醒/@唤醒/放行）")


    # ------------------------------------------------------------ 出站拦截（工具/标记）
    silent_tools = st._split_triggers("stay_silent,no_reply,silent", st.DEFAULT_SILENT_TOOLS)
    silent_marks = st._split_triggers("[[SILENT]],[[silent]],[[静默]]", st.DEFAULT_SILENT_MARKS)

    tool_cases = [
        (["stay_silent"], True),
        (["Gareth/GroupSpeakerTag/stay_silent"], True),
        (["get_weather"], False),
        ([], False),
    ]
    for funcs, expect in tool_cases:
        got = st._silent_tool_called(funcs, silent_tools)
        assert got == expect, f"funcs={funcs}: 期望 {expect}，实际 {got}"

    mark_cases = [
        ("[[SILENT]]", None),
        ("好的[[SILENT]]", "好的"),
        ("[[SILENT]] 好的", "好的"),
        ("（安静）", "（安静）"),
        ("灵灵在呢喵", "灵灵在呢喵"),
    ]
    for text, expect in mark_cases:
        got = st._strip_silent_marks(text, silent_marks)
        assert got == expect, f"{text!r}: 期望 {expect!r}，实际 {got!r}"

    print(f"[14] 出站拦截通过: 工具命中 {len(tool_cases)} 例、标记剥离 {len(mark_cases)} 例")


asyncio.run(main())
server.shutdown()

print(f"\n全部通过（langbot-plugin {SDK_VERSION}）：插件可加载、取配置、注册事件、")
print("纯文本打标、图片本地转 base64、下载失败降级，且从不把平台 URL 交给模型。")
