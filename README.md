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
| 群里太吵想让它闭嘴 | 只能改触发规则或关机器人 | 发「灵灵安静」即静默拦截，发「灵灵」恢复 |

> 注意：`from_image_url` 在本插件中**被明确禁止**。QQ CDN 链接带一次性 `rkey` 与来源校验，
> 第三方服务端抓不到，直接透传 URL 会导致模型侧 `Failed to download image` 400。

## 安装

1. 下载 `GroupSpeakerTag-0.5.0.lbpkg`
2. 面板 → 添加扩展 → 上传该文件（若装着旧版，先卸载再装；手传包没有"升级"按钮）
3. 面板 → 流水线 → 插件/扩展设置，**把插件绑定到这条流水线**（漏了等于没装）
4. 群里 `@机器人 !whoami` 验证

完整说明、配置项、FAQ 见 **[GroupSpeakerTag/README.md](GroupSpeakerTag/README.md)**。

## 目录结构

```
GroupSpeakerTag/                            插件源码（manifest.yaml 在根）
  components/event_listener/speaker_tag.py  全部逻辑
GroupSpeakerTag-0.5.0.lbpkg                 发布包，可直接上传安装
GroupSpeakerTag-离线自检.py                  12 项断言的离线验证脚本
VERSIONS.md                               历史版本索引（各版 lbpkg 下载 + SHA256）
patch-*.py                                三个可选补丁（静音 / 详细报错 / 历史消毒）
start-langbot.bat                         一键启动（锁定 langbot+sqlalchemy 组合）
QQ机器人_DeepSeek_保姆级部署指南.md           本机部署记录
```

## 版本与历史包

当前 **v0.5.0**（tag `v0.5.0`）。**每个版本的 `.lbpkg` 都跟着 git tag 存档，随时可回退下载**
→ 见 **[VERSIONS.md](VERSIONS.md)**（含直接下载链接与 SHA256）。

| 版本 | 主要变更 | 下载 |
|---|---|---|
| 0.5.0 | **静默开关**：「灵灵安静」→ 拦掉所有消息不再回复，「灵灵」/@ 唤醒；触发词可配置 | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.5.0/GroupSpeakerTag-0.5.0.lbpkg) |
| 0.4.4 | 更正：插件消毒层对 LocalAgent 无效，防线改由 `patch-history-sanitize.py` 承担 | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.4/GroupSpeakerTag-0.4.4.lbpkg) |
| 0.4.3 | 空消息补齐（空 content → `[空白消息]`） | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.3/GroupSpeakerTag-0.4.3.lbpkg) |
| 0.4.2 | 自检报告区分“配置为空”与“读取失败”，显示历史图片消毒计数 | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.2/GroupSpeakerTag-0.4.2.lbpkg) |
| 0.4.1 | 请求前清洗历史图片（远程链接 / 空 base64 → `[图片]`）⚠ 实测对 LocalAgent 无效 | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.1/GroupSpeakerTag-0.4.1.lbpkg) |
| 0.4.0 | 发言人标记 + 图片本地转 base64 + QQ 表情文本化 + `!whoami` / `!prompt` | [lbpkg](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.0/GroupSpeakerTag-0.4.0.lbpkg) |

> v0.1.0 ~ v0.3.0 是建仓前的开发中间产物，**未进 git**，无法回溯。

其他版本形态：

```bash
# 拉某个版本的完整源码
git clone --branch v0.4.1 --depth 1 https://github.com/block-Zj/GroupSpeakerTag.git
```

## 兼容性

- `langbot 4.10.x` + `langbot-plugin 0.5.8`
- `langbot 4.11.x` + `langbot-plugin 0.7.11`

配置读取走三路兜底（`get_plugin_config` → `plugin.get_config` → `plugin.config`），
两代 SDK 均已离线验证通过。

## 配套补丁

LangBot 把上游模型的原始报错吃掉了，只留一句 `Invalid request parameters`。两个可选补丁：

| 脚本 | 作用 |
|---|---|
| `patch-silent-runner-errors.py` | runner 出错不再往群里发提示（错误只进日志/面板） |
| `patch-detailed-errors.py` | 保留上游原始报错，日志里能看到真实 400 原因 |
| `patch-history-sanitize.py` | **核心**：在 Transcript 读取入口清掉失效图片/空消息，从根上挡 400 |

都支持 `--dry-run` / `--revert`，升级 LangBot 或清 uv 缓存后重跑一次即可。

## 开发

```bash
# 用 LangBot 自己的解释器跑离线自检（最贴近真实运行时）
"<uv cache>/archive-v0/<hash>/Scripts/python.exe" GroupSpeakerTag-离线自检.py
```

改完代码重新打包后，应确认 `transcript.content_json` 中不含 `"image_url": {"url"` ——
否则旧历史会在每轮请求里被重放，导致该会话持续报错。
