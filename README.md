# GroupSpeakerTag

LangBot（v4）插件：让 QQ 群里的 AI 认得出**谁在说话**、看得见**图片**、也读得懂**表情**。

适用环境：LangBot 4.10.x / 4.11.x（`uvx langbot` 或 Docker）+ NapCat（OneBot v11）。

## 它解决什么问题

| 问题 | 原生表现 | 装上之后 |
|---|---|---|
| 群聊中分不清发言人 | 会话键里没有 `sender_id`，全群共用一段匿名历史，AI 会把不同人认成同一个 | 每条消息前加 `【昵称（QQ:号）】`，且**在落库前改写**，多轮之后历史仍可区分 |
| QQ 图片识别不了 | 4.11 新适配器不填 `base64`，内核只发 `base64`，图片被静默丢弃 | 本机下载图片转 `data:` URI，`image-mode` 可选 `auto` / `skip` |
| QQ 表情被吃掉 | 内核 `preproc` 里没有 `Face` 分支 | `[表情]微笑`、`[表情]5点的骰子`、`[表情]猜拳(剪刀)`、`[动画表情]` |
| `!prompt` / `!reset` 等命令报"未知命令" | v4 已移除全部内置命令 | 插件自带 `!whoami`（自检）与 `!prompt`（提示词快照） |

> 注意：`from_image_url` 在本插件中**被明确禁止**。QQ CDN 链接带一次性 `rkey` 与来源校验，
> 第三方服务端抓不到，直接透传 URL 会导致模型侧 `Failed to download image` 400。

## 安装

1. 下载 `GroupSpeakerTag-0.4.0.lbpkg`
2. 面板 → 添加扩展 → 上传该文件（若装着旧版，先卸载再装；手传包没有"升级"按钮）
3. 面板 → 流水线 → 插件/扩展设置，**把插件绑定到这条流水线**（漏了等于没装）
4. 群里 `@机器人 !whoami` 验证

完整说明、配置项、FAQ 见 **[GroupSpeakerTag/README.md](GroupSpeakerTag/README.md)**。

## 目录结构

```
GroupSpeakerTag/                            插件源码（manifest.yaml 在根）
  components/event_listener/speaker_tag.py  全部逻辑
GroupSpeakerTag-0.4.0.lbpkg                 发布包，可直接上传安装
GroupSpeakerTag-离线自检.py                  10 项断言的离线验证脚本
QQ机器人_DeepSeek_保姆级部署指南.md           本机部署记录
```

## 版本

当前 **v0.4.0**（tag `v0.4.0`）。

| 版本 | 主要变更 |
|---|---|
| 0.1.0 | 发言人标记 |
| 0.2.x | 修 SDK 版本兼容（`get_plugin_config` 在 0.5.8 上不存在）、加 `!whoami` |
| 0.3.0 | 图片改本地转 base64，移除 `from_image_url` |
| 0.4.0 | QQ 表情 / 骰子 / 猜拳 / 表情包文本化 |

## 兼容性

- `langbot 4.10.x` + `langbot-plugin 0.5.8`
- `langbot 4.11.x` + `langbot-plugin 0.7.11`

配置读取走三路兜底（`get_plugin_config` → `plugin.get_config` → `plugin.config`），
两代 SDK 均已离线验证通过。

## 开发

```bash
# 用 LangBot 自己的解释器跑离线自检（最贴近真实运行时）
"<uv cache>/archive-v0/<hash>/Scripts/python.exe" GroupSpeakerTag-离线自检.py
```

改完代码重新打包后，应确认 `transcript.content_json` 中不含 `"image_url": {"url"` ——
否则旧历史会在每轮请求里被重放，导致该会话持续报错。
