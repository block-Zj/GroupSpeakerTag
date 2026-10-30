# 历史版本

每个版本都有独立的 **git tag**，包体（`.lbpkg`）跟着 tag 一起存档，随时可以下回来。

## 快速下载

| 版本 | 日期 | 包大小 | 直接下载 | SHA256（前 16 位） |
|---|---|---|---|---|
| **v0.7.0**（最新） | 2026-10-30 | 30.4 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.7.0/GroupSpeakerTag-0.7.0.lbpkg) | `efea9f1f4a54bee2` |
| v0.6.0 | 2026-10-30 | 27.6 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.6.0/GroupSpeakerTag-0.6.0.lbpkg) | `443b3f5d3989af2c` |
| v0.5.0 | 2026-10-22 | 25.1 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.5.0/GroupSpeakerTag-0.5.0.lbpkg) | `35953eec624281e5` |
| v0.4.4 | 2026-10-21 | 21.8 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.4/GroupSpeakerTag-0.4.4.lbpkg) | `644adee1ca331ba0` |
| v0.4.3 | 2026-10-21 | 20.0 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.3/GroupSpeakerTag-0.4.3.lbpkg) | `b6f11d497d5c351f` |
| v0.4.2 | 2026-10-21 | 19.0 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.2/GroupSpeakerTag-0.4.2.lbpkg) | `4d211b53f0d7d1f3` |
| v0.4.1 | 2026-10-19 | 18.5 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.1/GroupSpeakerTag-0.4.1.lbpkg) | `58270e95797f95c6` |
| v0.4.0 | 2026-10-19 | 16.0 KB | [下载](https://github.com/block-Zj/GroupSpeakerTag/blob/v0.4.0/GroupSpeakerTag-0.4.0.lbpkg) | `4f2898524a133262` |

> 点链接会打开该版本的文件页，右上角 **Download raw file** 即可下载。
> 之所以不直接给 `raw.githubusercontent.com` 直链：那个域名在国内经常连不上，
> `github.com` 反而稳定。
> 校验：`certutil -hashfile 文件名 SHA256`（Windows）。

## 每个版本改了什么

| 版本 | 主要变更 |
|---|---|
| **v0.7.0** | 改用 `NormalMessageResponded` 事件拦截出站回复：模型调用 `stay_silent` 或只输出静默标记时整条不发。修 0.6.0 在 respback 层漏判的问题。 |
| v0.6.0 | 新增 `stay_silent` 工具：模型用**工具调用**表达「不回复」，而不是写 `[[SILENT]]` 文字。配套补丁在发送阶段拦截。 |
| v0.5.0 | 静默开关：群里出现「灵灵安静」即拦截全部消息、不再回复；发「灵灵」或 @她 恢复。触发词可配置。 |
| v0.4.4 | 更正文档：说明插件内的「请求前消毒」对 LocalAgent 无效（runner 直读 Transcript），真正的防线改为配套补丁 `patch-history-sanitize.py`。代码行为与 0.4.3 一致。 |
| **v0.4.3** | 空消息补齐：`content` 为空数组 / 空串 / 纯空白 → `[空白消息]`。修 `Invalid request parameters` 的一个来源。 |
| **v0.4.2** | 自检报告修正：区分「配置为空」与「读取失败」，新增「历史图片消毒」计数。 |
| **v0.4.1** | 请求前清洗历史图片（远程 `image_url` / 空 `image_base64` → `[图片]`）。⚠ 实测对 LocalAgent 无效，见 v0.4.4 说明。 |
| **v0.4.0** | 首个归档版：发言人标记 + 图片本地转 base64 + QQ 表情文本化 + `!whoami` / `!prompt` 命令。 |

> v0.1.0 ~ v0.3.0 是开发中的中间产物，**没有进 git**（仓库是 0.4.0 时才建的），
> 所以只有 0.4.0 及之后能回溯。

## 想下某个版本的完整源码

两种方式：

**① 用 tag 浏览**

```
仓库首页 → Branch 下拉框旁的 "Tags" → 选版本 → Code → Download ZIP
```

**② 命令行**

```bash
git clone --branch v0.4.1 --depth 1 https://github.com/block-Zj/GroupSpeakerTag.git
```

`--depth 1` 只拉那一个版本，快。

## 发版约定（给自己看的）

1. 改完代码 → 重新打包 `GroupSpeakerTag-X.Y.Z.lbpkg` → 删掉上一版的包
2. `manifest.yaml` 里的 `version` 与 `PLUGIN_VERSION` 同步改
3. 提交 + 打附注标签：`git tag -a vX.Y.Z -m "..."`，然后 `git push origin main vX.Y.Z`
4. **不要删旧 tag** —— 删了对应版本的包就没了，上表链接会 404

> 如果以后想要 GitHub 的 **Releases** 页面（比 tag 更好找、带更新说明），
> 需要给仓库装有写权限的 token 后用 API 创建，或者手动在 web 上
> 「Releases → Draft a new release → 选 tag → 上传对应的 .lbpkg」。
