# GroupSpeakerTag · 群发言人标记插件

解决三件事：

1. **AI 分不清群里谁在说话** —— 给每条群消息加上「发言人昵称 + QQ 号」前缀。
2. **QQ 图片识别不了 / 报 `Failed to download image`** —— 在本地把图片下载成 base64 再交给模型。
3. **QQ 表情（小黄脸 / 骰子 / 猜拳 / 表情包）AI 看不见** —— 渲染成「[表情]微笑」这类文本。

适用：LangBot 4.10.x / 4.11.x（`uvx langbot` 或 Docker）+ NapCat（OneBot v11）接 QQ。

当前版本：**0.7.0**

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

## 二·七、v0.4.2：自检报告修正

`!whoami` 的两处显示问题，不影响功能，但会误导判断：

| 旧输出 | 问题 | 新输出 |
|---|---|---|
| `配置读取：未取到，正在用默认值` | 配置**读到了**、只是内容为空（全默认），却被报成失败 | `配置读取：正常（component.get_plugin_config()）；当前配置为空，全部按默认值运行` |
| 没有消毒计数 | 看不出安全网有没有在工作 | `历史图片消毒：累计 N 处 → [图片]（最近：…）` |

`_plugin_config()` 现在把读取结果记进 `_CONFIG_STATE`（`loaded` / `source` / `error`），
并且每次调用先重置，所以"空配置"和"读不到"是两种明确的结论，不再混为一谈。

## 二·八、v0.4.3：空消息补齐（`Invalid request parameters` 的另一个源头）

### 症状

整个会话每条消息都失败，面板上只有：

```
Model request failed: Invalid request parameters
```

**跟发什么内容无关**，纯文字也失败；过几分钟又自己好了。

### 根因

transcript 里存在**内容为空的消息**：

```json
{"role": "user", "content": []}          // 纯图片消息在旧版本里没转换成功
{"role": "user", "content": [{"type": "text", "text": " "}]}
```

LangBot 4.11 会把它们原样重放进请求。DeepSeek 收到空 content 直接返回 400，
于是**一条空消息能让该会话连续失败**，直到它被上下文窗口挤出去才"自愈"——
这就是"过一会儿又好了"的原因。

### 0.4.3 的做法

`PromptPreProcessing` 里在图片消毒之后再扫一遍消息：

| 情况 | 处理 |
|---|---|
| `content == []` | 换成 `[空白消息]` |
| `content` 是空字符串 / 纯空白 | 换成 `[空白消息]` |
| 元素只有空白 `text` | 换成 `[空白消息]` |
| 含图片/文件的元素 | **不动**（那是有效内容） |
| 已经是 `[空白消息]` | 跳过（幂等） |

`!whoami` 会多一行 `空消息补齐：累计 N 处 → [空白消息]`。

> 顺带说明：LangBot 4.11 把 `litellm.BadRequestError` 统一映射成
> `Invalid request parameters`（`litellmchat.py` 的映射表），**上游原文被丢弃**。
> 想知道真实原因，用仓库根的 `patch-detailed-errors.py` 打一个补丁，
> 之后日志里会带上 DeepSeek 返回的 `error.message`。

## 二·九、v0.4.4：更正 —— 插件里的"消毒层"对 LocalAgent 无效

这是我必须纠正的一个判断。v0.4.1 ~ v0.4.3 都声称"在请求发出前清洗历史"，
**实测下来对实际发出去的请求没有作用**。

### 证据（LangBot 源码注释）

`pkg/agent/runner/transcript_store.py:274-282`：

```python
def load_history(...):
    """Project Transcript rows into the legacy provider Message view.

    Runner history is canonical in Transcript. This view exists for
    legacy Pipeline readers such as PromptPreProcessing that still expect
    query.messages.
    """
```

翻成大白话：**AgentRunner（LocalAgent）直接读 Transcript 表**，
`PromptPreProcessing` 拿到的 `query.messages` 只是给旧管线看的兼容视图。
插件在里面改得再干净，也只改了那个视图，模型照样收到原始的行。

### 为什么之前看起来"有效"

因为每次都是**同时**做了数据库清理。真正起作用的是清库，不是消毒层。
判断依据很清楚：13:59 那次失败与 14:05 清库之后成功之间，进程、插件、配置都没变，
唯一变化就是库里那 5 条 `image_url` 被换成了 `[图片]`。

### 真正的防线

用仓库根的 **`patch-history-sanitize.py`**，它打两个点：

| 位置 | 作用 |
|---|---|
| `transcript_store._transcript_item_to_provider_message()` | **历史投影的必经之路**，在这里把 `image_url` / 空 `image_base64` / `file_*` / 空内容降级成文本 |
| `query_entry_adapter.py` 的 `if not contents` 回退分支 | 图片只有 url 没有 base64 时，原本会生成 `image_url`（毒源），改成输出 `[图片]` |

插件里的消毒代码保留着（对旧管线路径有效），但**不要再指望它挡 400**。

## 五·五、静默开关（v0.5.0）

### 怎么用

```
群里有人说：灵灵安静        → 插件拦掉这条，并进入静默
（之后群里说什么她都不回，消息根本不进流水线，不烧 token）
有人说：灵灵 / @灵灵 一句   → 立即恢复，这条消息正常处理
```

| 配置项 | 默认 | 说明 |
|---|---|---|
| `mute-triggers` | `灵灵安静` | 出现即静默，多个用逗号分隔（如 `灵灵安静,安静灵灵`） |
| `wake-triggers` | `灵灵` | 静默期间出现即恢复；@机器人 无条件唤醒 |
| `bot-qq` | 空 | 填机器人 QQ 后只认 @她；留空则任意 @ 都算唤醒 |
| `mute-ack` | 空 | 进入静默时回一句（如「灵灵先不说话了喵」）；留空完全不出声 |

### 为什么这次是真拦得住

`GroupMessageReceived` 由 `pipelinemgr` 在**进入流水线各阶段之前**发出：

```python
event_ctx = await self.ap.plugin_connector.emit_event(event_obj, bound_plugins)
if event_ctx.is_prevented_default():
    logger.debug('MessageReceived event prevented default ...')
    return                      # ← 直接 return，后面的触发规则/模型/回复全都不执行
query.message_chain = event_ctx.event.message_chain
await self._execute_from_stage(0, query)
```

所以插件里调用 `event_context.prevent_default()` 是**真拦**：
消息不会进触发规则、不会调模型、不会有任何回复，连监控里都不会记一条。

> 对比：`PromptPreProcessing` 是流程后半段的钩子，只能改请求内容，拦不住流程
> （这也是 v0.4.x 消毒层无效的原因，见上一节）。

### 边界

- **静默状态只存内存，不落盘。** 重启 LangBot 自动恢复说话，避免"忘了自己关过"。
- 静默期间 `!whoami` 等命令也会被拦（因为整条消息都不进流水线）。
  想查状态：先发「灵灵」恢复，或者直接看 `!whoami`。
- 唤醒只是"解除静默"，是否真的回你，仍由流水线的触发规则（@ / 前缀 / 随机）决定。
- 静默按**群**生效，不同群互不影响。

## 五·六、让模型自己决定不说话（v0.6.0）

### 问题：`[[SILENT]]` 是一段文字，不是一个动作

模型从别的 AI 那里学来 `[[SILENT]]` 这种写法，以为写上就等于"我不说话"。
但**文字对系统没有任何意义**——不写补丁的话，群里看到的就是这四个字符；
写了补丁也只是"事后按字符串匹配"，模型并不知道自己"做对了一件事"。

### 做法：给它一个真正的工具

`components/tool/stay_silent.yaml` 注册了一个工具 `stay_silent`：

| 项 | 内容 |
|---|---|
| 工具名 | `stay_silent`（模型看到的函数名） |
| 参数 | `reason`（可选，仅写入日志） |
| llm_prompt | 明确告诉模型：**判断不该接话时调用本工具，并严禁用 `[[SILENT]]` 之类文字代替** |

模型调用后，`call()` 会：

1. 返回一句"已记录：本轮不回复，现在不要输出任何文字"；
2. 在 `%TEMP%/langbot-silent/<query_id>.lock` 落一个标记（兜底用）。

配套补丁 `patch-silent-reply.py`（v2）在发送阶段判断：

```
resp_messages 里有 stay_silent 的 tool_call   → 整条丢弃
或 存在本轮静默标记                            → 消费并丢弃
或 回复文本只剩 [[SILENT]] 这类标记            → 丢弃（老习惯的兼容）
```

也就是说：**模型调工具 = 真的不说话；顺手写的 `[[SILENT]]` 也仍然兜得住。**

### 为什么插件自己拦不住

插件事件全是入站的（`MessageReceived` / `PromptPreProcessing` …），**没有"发送前"事件**，
所以"吞掉这条回复"必须由 `respback` 阶段的补丁完成。工具负责"让模型能表达"，
补丁负责"让表达生效"。

### 验收

1. 装 0.6.0 → 重启 LangBot → 确认 0.5.0 的 `patch-silent-reply.py` 已升级到 v2
   （`python patch-silent-reply.py` 会显示"已升级到 v2"）；
2. 群里聊一句它不该接的话；
3. 日志应出现 `Silent reply suppressed for query N`，群里没有任何消息。

> 如果模型仍然只写 `[[SILENT]]` 而不调工具，说明它还没学会新习惯。
> 可以在系统提示词里加一句："如果要保持安静，调用 stay_silent 工具。"
> 字符串兜底会保证此时也不会把 `[[SILENT]]` 发出去。

## 五·七、v0.7.0：用 `NormalMessageResponded` 真正拦住出站回复

### 上一版为什么没兜住

v0.6.0 的拦截打在 `respback` 阶段，只看 `query.resp_message_chain[-1]`。
一旦本轮有工具调用，`wrapper` 会往 `resp_message_chain` 里追加**多条**链
（例如 "Calling stay_silent..." 的提示链、最终文案链），
`[-1]` 未必是最终文案；而工具调用记录在 `resp_messages` 的 assistant 消息上，位置也不固定。
结果是：工具确实被调用了，但发出去的那条没被判定到。

### 正确的钩子：`NormalMessageResponded`

`pkg/pipeline/wrapper/wrapper.py` 在**构造回复链时**触发这个事件，它把本轮信息都给了插件：

```python
event = events.NormalMessageResponded(
    ...,
    response_text=reply_text,
    funcs_called=[fc.function.name for fc in result.tool_calls] or [],
    query=query,
)
event_ctx = await self.ap.plugin_connector.emit_event(event, bound_plugins)
if event_ctx.is_prevented_default():
    yield entities.StageProcessResult(result_type=entities.ResultType.INTERRUPT, ...)
```

所以插件里 `event_context.prevent_default()` 会让流水线 **INTERRUPT**，消息根本不发出。

### 现在的判定规则（按顺序）

| 情况 | 处理 |
|---|---|
| `funcs_called` 里出现 `silent-tools` 中的工具（默认 `stay_silent,no_reply,silent`） | 整条不发 |
| 回复文本**只剩**静默标记（默认 `[[SILENT]]` 等） | 整条不发 |
| 标记混在正文里 | 剥掉标记，发送剩下的正文 |
| 都没有 | 原样发送 |

工具名允许带归属前缀（`Gareth/GroupSpeakerTag/stay_silent` 也认）。

### 配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| `silent-tools` | `stay_silent,no_reply,silent` | 调用了这些工具就整条不发 |
| `silent-marks` | `[[SILENT]],[[silent]],[[静默]]` | 纯文字标记兜底；可按需加 `（安静）` 这类舞台提示 |

`!whoami` 会多一行：`模型主动不说话：累计 N 次（最近：调用了工具 / 只剩标记 / 剥掉标记后发送）`。

> v0.6.0 的 `respback` 补丁可以保留当第二道保险（插件没绑定时仍有作用），
> 但正常路径已经由本事件接管。

---

## 三、安装

### 方式 A（推荐）：网页上传 .lbpkg

1. 面板 `http://127.0.0.1:5300` → **添加扩展**（Add Extension）。
2. 上传 `GroupSpeakerTag-0.7.0.lbpkg`。
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
| `mute-triggers` | `灵灵安静` | 静默触发词，多个用逗号分隔 |
| `wake-triggers` | `灵灵` | 唤醒词，多个用逗号分隔；@机器人 也算唤醒 |
| `bot-qq` | 空 | 填机器人 QQ 后，只有 @她 才算唤醒；留空则任意 @ 都算 |
| `mute-ack` | 空 | 进入静默时回一句确认；留空 = 完全静默 |

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
【发言人标记 · 自检 v0.7.0】
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

