# GroupSpeakerTag · 群发言人标记插件

解决三件事：

1. **AI 分不清群里谁在说话** —— 给每条群消息加上「发言人昵称 + QQ 号」前缀。
2. **QQ 图片识别不了 / 报 `Failed to download image`** —— 在本地把图片下载成 base64 再交给模型。
3. **QQ 表情（小黄脸 / 骰子 / 猜拳 / 表情包）AI 看不见** —— 渲染成「[表情]微笑」这类文本。

适用：LangBot 4.10.x / 4.11.x（`uvx langbot` 或 Docker）+ NapCat（OneBot v11）接 QQ。

当前版本：**0.4.1**

---

## 一、它做了什么

LangBot 的会话粒度是**群**：同一个群里所有成员共用一份对话历史，而送进模型的 user 消息
只有正文、没有发送者身份。所以多轮之后模型只能盲猜是谁在说话。

本插件在消息进入模型之前改写用户消息：

```
改写前：  今天下午几点开会？
改写后：  【张三（QQ:10001）】今天下午几点开会？
```

改写发生在 `user_message_alter` 阶段，这句话**同时**会写进对话历史，
所以后续轮次里模型依然能看到每条历史消息的发言人。

另外会注入一条系统说明，告诉模型这段 `【…】` 是系统标记、不是用户正文。

---

## 二、v0.3.0 修了什么（重要）

### 症状

```
litellm.BadRequestError: DeepseekException -
{"error":{"message":".messages[8].image[0]: Failed to download image from
  https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=...&rkey=..."}}
```

### 根因（三层）

**① LangBot 4.11 的 QQ 适配器不填 base64**

`langbot/pkg/platform/adapters/aiocqhttp/message_converter.py`

```python
platform_message.Image(
    image_id=str(segment.data.get('file', '')),
    url=segment.data.get('url') or segment.data.get('file') or '',
)          # ← 没有 base64 参数
```

**② 内核只认 base64，没有就静默丢图**

`langbot/pkg/pipeline/preproc/preproc.py`

```python
elif isinstance(me, platform_message.Image):
    if keep_image_inputs:
        if me.base64 is not None:                       # ← 只有这一条路
            content_list.append(from_image_base64(me.base64))
```

base64 为 None 时什么都不加 —— 连 `[Image]` 占位都没有。
（旧实现 `pkg/utils/image.py: qq_image_url_to_base64` 已被官方标注 `[弃用]`。）

**③ 0.2.x 的插件把平台 URL 直接交给了模型**

```python
return [ContentElement.from_image_url(component.url)]   # ← 0.2.x 的写法，已删除
```

QQ 的 `multimedia.nt.qq.com.cn/...&rkey=...` 是**一次性签名 + 来源校验**的链接，
你本机能下载，模型服务商的服务器一定下不了 → 400。

### 0.3.0 的做法

```
图片 → 有 base64？直接用
     → 只有 url？用 urllib 在本地下载（UA/Referer 伪装、10s 超时、8MB 上限）
                 → 成功：转 data:image/xxx;base64,... 交给模型
                 → 失败：降级成「[图片]」文本
```

**铁律：永远不用 `from_image_url`。** 平台给的临时链接只能本地消化。

纯文本模型（不支持视觉）可以把 `image-mode` 设成 `skip`，直接占位、不浪费流量。

---

## 二·五、v0.4.0：QQ 表情

### 症状

群里发一个 QQ 表情（小黄脸），机器人像是没看见——不评论、不回应，
或者干脆把同一句话当成了纯文字理解。

### 根因

**适配器解析了，内核丢了。**

LangBot 4.11 的 aiocqhttp 适配器其实把表情整理得很好
（`message_converter.py`）：

```python
elif segment.type == 'face':
    face_id = str(segment.data.get('id', 0))
    raw = segment.data.get('raw')
    face_name = str(raw.get('faceText') or '') if isinstance(raw, dict) else ''
    components.append(Face(
        face_id=int(face_id or 0),
        face_name=face_name.replace('/', '') or FACE_NAMES.get(face_id, ''),
    ))
```

适配器内置了 id→中文名映射表 `FACE_NAMES`（`14`=微笑、`182`=笑哭 ……），
`rps`（猜拳）、`dice`（骰子）也一并归到 `Face`。SDK 里的 `Face.__str__` 更好用：

| face_type | `str(Face)` |
|---|---|
| `face` | `[表情]微笑` |
| `dice` | `[表情]5点的骰子` |
| `rps` | `[表情]猜拳(石头)` |

**但内核 `pkg/pipeline/preproc/preproc.py` 里没有任何 `Face` 分支**——
构建 `content_list` 时只处理 Plain / Image / Voice / File / Quote，
表情直接被跳过，模型自然看不到。

顺带一个隐患：**纯表情消息**在旧逻辑下会变成 `content` 为空的 user 消息
（可能触发模型侧 400）。补上 Face 分支后不会再有空消息。

### 0.4.0 的做法

```python
if isinstance(component, platform_message.Face):
    return [ContentElement.from_text(_render_face(component))]
```

渲染规则（比 SDK 的 `__str__` 多一层兜底，避免未知 id 崩掉）：

| 类型 | 输出 |
|---|---|
| `face` + 有名字 | `[表情]微笑` |
| `face` + 无名字 | `[表情:178]` |
| `dice` | `[表情]5点的骰子` |
| `rps` | `[表情]猜拳(剪刀)` |

另外把 `Unknown` 段也做了收敛：适配器对未处理的段（比如 `mface` 表情包）
会生成 `Unknown(text='mface:{"summary": "[动画表情]", "url": "https://..."}')`，
插件会从中抽出 `summary` 输出 `[动画表情]`，**并把长 URL 剔掉**，
不让它污染上下文。

---

## 二·六、v0.4.1：历史图片消毒（重要）

### 症状

某个群**每条消息**都回 `Agent runner temporarily unavailable. Please try again.`，
日志里是：

```
.messages[31].image[0]: Failed to download image from https://multimedia.nt.qq.com.cn/...
```

而换个群、或清空历史之后又一切正常。**`messages[N]` 的 N 每次都不一样**——
这是最关键的线索：坏的不是新消息，是历史里某条记录随着窗口滑动在换位置。

### 根因：LangBot 4.11 会让"图片进入历史"这一步产生两种毒记录

**① 旧的远程链接（历史遗留）**

QQ 图片链接是 `multimedia.nt.qq.com.cn/...&rkey=...`，一次性签名 + 来源校验。
历史里只要存了 `{"type": "image_url", "image_url": {"url": "https://multimedia..."}}`，
之后**每一轮请求**都会把它原样发给模型；服务商服务器下载必然失败 → 400 → 整轮失败。

**② 空图片元素（4.11 的落库策略导致）**

`pkg/agent/runner/run_journal.py:42-53` 在写 transcript 时会把 base64 抹掉：

```python
if item_type == 'image_base64' and item.get('image_base64'):
    item['image_base64'] = None
    item['content_redacted'] = True
```

于是历史里躺着一堆 `{"type": "image_base64", "image_base64": null}`。
请求层（`litellmchat.py:715-745`）会 `continue` 掉它们，不报错但**图片信息彻底丢失**。

两者叠加的结果：**只要某个会话发过图，那段历史就变成不可靠状态**——
一条坏记录能让该会话永久失败，而且只影响发言最活跃的那个群。

### 0.4.1 的做法

在 `PromptPreProcessing` 里，请求发出前把整段上下文过一遍（此时 `event.prompt` 可改写，
宿主在 `preproc.py:429` 会用 `query.messages = event_ctx.event.prompt` 接收修改）：

| 元素 | 处理 |
|---|---|
| `image_url` 且是 `http(s)` 链接 | 换成 `[图片]` |
| `image_url` 且是 `data:` URI | **原样保留**（合法，不用清洗） |
| `image_base64` 有载荷 | 原样保留 |
| `image_base64` 为空 | 换成 `[图片]` |
| `file_url` / `file_base64` | 换成 `[文件]` |

这样即使数据库里还留着坏记录，也**不可能再让请求 400**。
`!whoami` 里会显示累计清洗数量和最近一次原因。

> 换了这版之后，历史里有没有毒记录都不影响使用；
> 但先把数据库清一遍更干净（见下面「清理历史」）。

### 清理历史（可选，建议做）

判据一：还有没有远程图片链接

```sql
select count(*) from transcript where content_json like '%"image_url": {"url"%';
```

判据二：还有没有空图片元素

```sql
select count(*) from transcript where content_json like '%content_redacted%';
```

两条都应为 `0`。不为 0 时，把 `image_url` / 空 `image_base64` 元素就地换成
`{"type": "text", "text": "[图片]"}` 即可，**行数、角色、文字内容都不要动**。
动手前先备份（LangBot 运行中也能安全备份）：

```python
import sqlite3
src = sqlite3.connect(r"D:\Desktop\Ling\langbot\data\langbot.db")
dst = sqlite3.connect(r"D:\Desktop\Ling\langbot\data\langbot.db.bak_" + ts)
src.backup(dst)
```

---

## 三、安装

### 方式 A（推荐）：网页上传 .lbpkg

1. 面板 `http://127.0.0.1:5300` → **添加扩展**（Add Extension）。
2. 上传 `GroupSpeakerTag-0.4.1.lbpkg`。
   - 如果你还装着 0.2.x / 0.3.x，**先卸载旧版再装**（手传的包没有"升级"按钮），
     否则可能出现两个版本并存。
3. 面板 → **插件**，确认已启用。
4. 面板 → **流水线** → 插件/扩展设置，把 `GroupSpeakerTag` 绑定到流水线。
   **这步不能漏**——没绑定到流水线的插件不会生效。
5. 群里 @ 机器人发 `!whoami` 自检。

> `.lbpkg` 就是插件目录打成 zip，`manifest.yaml` 必须在归档根目录。
> 无签名插件在自部署（OSS）下判定为 `OSS_LEGACY_DEDICATED`，允许安装。

### 方式 B（兼容/调试）：文件夹丢进 data/plugins

复制 `GroupSpeakerTag` 目录到 `<LangBot 工作目录>\data\plugins\GroupSpeakerTag\`，
然后**重启 LangBot**（扫描只在启动时做，且无热重载）。
这条是 legacy 通道：不持久化 lbpkg、面板不能升级、云端模式（shared）完全不生效。

---

## 四、配置项

面板 → 插件 → GroupSpeakerTag：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `tag-format` | `【{name}（QQ:{id}）】` | 标记模板。`{name}` = 群名片/昵称，`{id}` = QQ 号。想简洁可改 `【{name}】` |
| `image-mode` | `auto` | `auto` = 本地下载转 base64（QQ 图片必须）；`skip` = 用「[图片]」占位（纯文本模型） |
| `inject-guide` | 开 | 追加系统说明，告诉模型标记的含义 |
| `tag-quote` | 开 | 成员用「回复」功能时，把被引用原文带上并标注来源 |
| `prompt-dump-scope` | `admin` | `!prompt` 权限：`admin`（管理员+私聊）/ `everyone` / `off` |

`tag-format` 里 `{name}` 取 **群名片优先、其次昵称、最后 QQ 号**（与 LangBot 内部一致）。

---

## 五、自检

> LangBot v4 已移除全部内置命令，命令只能由插件提供。所以 `!prompt`、`!reset` 之类
> 在原生 v4 上是"未知命令"，本插件自带两条。

| 命令 | 作用 | 权限 |
|---|---|---|
| `!whoami`（别名 `!speaker`） | 报告发言人称谓/ID、将要加的前缀、打标计数、**图片成功/失败计数与最近错误** | 所有人 |
| `!prompt` | 打印系统提示词 + 最近 8 条历史（带发言人标记） | 私聊任何人；群里默认仅管理员 |

用法：群里 `@机器人 !whoami`，或**私聊机器人直接发 `!whoami`**（私聊必进流水线，
群里要先满足触发规则 @ / 前缀 / 随机）。

`!whoami` 输出示例：

```
【发言人标记 · 自检 v0.4.1】
会话：group_123456　← 同群所有人共用这一份上下文
群名：XX交流群
本次发言人：张三　ID：10001
将要加的前缀：【张三（QQ:10001）】
标签模板：【{name}（QQ:{id}）】
身份说明注入：开
图片处理：auto
配置读取：成功
已打标消息数：12
最近一次实际加的前缀：【张三（QQ:10001）】（张三(10001)）
图片：成功 2 张 / 失败 0 张
```

发一张图再发 `!whoami`，看「图片：成功 N 张」有没有 +1、有没有 ⚠ 错误行。

---

## 六、已知边界

- **旧历史不追溯标记。** 插件只影响安装之后的对话。装完建议重置会话。
- **这是"标注"，不是"隔离"。** 群里所有人仍共用一份上下文，只是能分清谁是谁。
  要"每人一份独立上下文"，LangBot 核心把会话键写死为 `(bot, launcher_type, launcher_id)`，
  插件改不了，得改用 Dify/n8n 外部 Agent。
- **图片识别依赖模型支持视觉。** `deepseek-chat` 这类纯文本模型拿到 base64 也不会看，
  请换支持视觉的模型（Qwen-VL / GLM-4V / GPT-4o / Gemini 等），或把 `image-mode` 设 `skip`。
- **表情是"文字化"，不是"看懂图"。** 插件输出 `[表情]微笑`，模型知道情绪标签，
  但看不到表情图本身。表情包（`mface`）只能拿到 `summary`，通常就是 `[动画表情]`。
- **图片下载走本机网络。** 超时 10s、单张上限 8MB；失败会降级「[图片]」并记进 `!whoami`。
- **VPS / 无外网环境**下载不到 QQ 图片时，会降级为「[图片]」。
- 插件内部全部 try/except 兜底（含 `image_url` 已被彻底移除），出错时保持原消息，不阻断对话。

---

## 七、给官方的 issue 要点（可选）

**① QQ 图片进不了模型（4.11 回归）**

新版 aiocqhttp 适配器不填 `Image.base64`，而内核 preproc 只发 base64，
导致 **QQ 图片在 4.11 上无法进入模型上下文**（不装任何插件也一样，图片会被静默丢弃）。
旧的 `qq_image_url_to_base64` 已标 `[弃用]`，替代路径缺失。

**② QQ 表情被丢弃（长期存在）**

适配器已经把 `face` / `rps` / `dice` 解析成 `platform_message.Face` 并带中文名，
但 `pkg/pipeline/preproc/preproc.py` 的 `content_list` 构建里没有 `Face` 分支，
表情直接被跳过。纯表情消息还会让 `user_message.content` 变成空列表。
建议内核把 `Face` 按 `str(face)` 渲染成文本（SDK 已经实现好了）——
位置就在处理 `Voice` / `File` 的那几个 `isinstance` 分支旁边。

