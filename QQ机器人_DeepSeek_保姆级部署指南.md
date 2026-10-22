# QQ 群 AI 机器人搭建指南（NapCat + LangBot + DeepSeek）

> 目标：让一个 QQ 小号在群里被 @ 时，自动调用 DeepSeek 大模型回复消息。  
> 全程 Windows 本地部署，照着做即可跑通。

---

## 0. 架构总览（先看懂再动手）

```
  QQ 群消息
      │
      ▼
 ┌─────────────┐   OneBot WebSocket    ┌──────────────┐   HTTPS API    ┌──────────┐
 │   NapCat    │ ────────────────────► │   LangBot    │ ─────────────► │ DeepSeek │
 │ (登录QQ小号) │   ws://127.0.0.1:3001  │ (调度+记忆)  │  ←───────────── │  大模型   │
 └─────────────┘                        └──────────────┘   返回结果      └──────────┘
      ▲                                      │
      └─────────── 发送回复到 QQ 群 ─────────┘
```

**三个角色各管一件事：**

| 组件       | 作用               | 端口                     |
| -------- | ---------------- | ---------------------- |
| NapCat   | 让程序"接管"QQ 小号收发消息 | WebUI `6099`、WS `3001` |
| LangBot  | 接收消息、调大模型、回消息、管群 | 面板 `5300`              |
| DeepSeek | 提供 AI 大脑（API）    | 官方云服务                  |

**端口别搞混**：`6099` 是你扫码登录的地方，`3001` 是 NapCat 和 LangBot 之间通信的管道，`5300` 是 LangBot 的网页管理后台。

---

## 1. 准备清单（动手前先凑齐）

| 项目                  | 要求                         | 备注                |
| ------------------- | -------------------------- | ----------------- |
| 一台 Windows 电脑       | Win10/11，内存 ≥ 4G           | 需要**长期开机**，关了就掉线  |
| 一个 **QQ 小号**        | 别用主号                       | 非官方协议有风控风险，用不心疼的号 |
| 一个 DeepSeek API Key | 去 platform.deepseek.com 申请 | 详见第 2 步           |

> ⚠️ **风险提示（必须看）**：本方案走的是非官方接口，账号存在被限制登录的概率。务必用**小号**，别拿主号长期挂生产。新号建议登录后**闲置挂几天再拉群**，降低风控。

---

## 2. 申请 DeepSeek API Key

1. 打开 <https://platform.deepseek.com>，用手机号/邮箱注册登录。
2. 左侧「API Keys」→「创建 API Key」，起个名字，复制生成的 `sk-xxxx`（**只显示一次，存好**）。
3. 在「充值/用量」里充几块钱即可（DeepSeek 很便宜，个人用一个月通常几块钱量级）。

**模型名记一下**（后面配置要用）：

- `deepseek-chat` —— 日常对话，快、便宜，**默认用这个**
- `deepseek-reasoner` —— 推理更强、慢一点，复杂问题再切

---

## 3. 部署 NapCat（让程序接管 QQ 小号）

### 3.1 先装官方 QQ 客户端

NapCat 是基于官方 QQ 客户端内核的，所以**必须先装 QQ**。 

- 去 <https://im.qq.com> 下载 Windows 版 QQ（NT 架构的桌面版），安装并**先正常登录一次你的 QQ 小号**，确认能用后再退出。

> 注意：装完 NapCat 后，**不要同时**用桌面 QQ 客户端登录同一个号，会互相顶下线。

### 3.2 下载 NapCat

- 打开 NapCat 官方仓库 Releases 页：<https://github.com/NapNeko/NapCatQQ/releases>
- 下载 **Windows 版**（推荐带图形界面的 `NapCat.Shell` 版本），例如 `NapCat.Shell.zip`。
- **不要**下载 `NapCat.Shell.Windows.OneKey.zip`（那个内置的 QQ 下载链接已失效，会报 404）。
- 解压到一个固定目录（比如 `D:\NapCat`），路径里**别带中文和空格**。

### 3.3 启动 + 扫码登录

1. 双击 `launcher.bat` 启动（Windows 10 用 `launcher-win10.bat`）。
   - 想快速指定账号，可命令行执行：`launcher.bat -q 你的QQ号`
2. 启动后会弹出一个浏览器窗口（或命令行提示），打开 `http://127.0.0.1:6099/webui/` 进入 NapCat 管理界面。
3. 首次使用需要**扫码登录**：用手机 QQ 扫 WebUI 里显示的二维码，选你的 QQ 小号登录。
4. 登录成功后，WebUI 状态栏应显示账号**在线**。

> 首次进 WebUI 若要求填 token，去启动目录的 `config/webui.json` 里找（或看启动日志里打印的 token）。

### 3.4 配置 OneBot WebSocket（关键一步）

在 NapCat WebUI 里找到「**网络配置**」（Network），新建一个 **WebSocket 服务端**（正向 WS）：

| 配置项                    | 填什么                                    |
| ---------------------- | -------------------------------------- |
| 监听地址                   | `127.0.0.1`                            |
| 端口                     | `3001`                                 |
| 消息格式 messagePostFormat | **`array`**（必须选 array，否则 LangBot 解析不了） |
| Access Token           | 可留空（本机通信）；要填就记住，LangBot 那边要一致          |

保存后**重启 NapCat**（或点应用）让配置生效。

**验证是否成功**：PowerShell 里执行

```powershell
Get-NetTCPConnection -LocalPort 3001 -State Listen
```

有输出（说明 3001 端口在监听）即成功。这个 NapCat 窗口**不要关**，关了机器人就下线。

---

## 4. 部署 LangBot（AI 调度大脑）

LangBot 有几种装法，Windows 上**推荐方式 A（一条命令）**。

### 方式 A：用 uv 一条命令启动（推荐）

1. 装 `uv`（Python 包管理器）。打开 PowerShell 执行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

装完重开一个终端，验证：`uv --version`

1. 建个专用目录并启动 LangBot（数据会存在这个目录，别乱删）：

```powershell
mkdir D:\langbot
cd D:\langbot
uvx langbot
```

首次运行会自动下载依赖，耐心等。看到日志提示后，浏览器打开 `http://127.0.0.1:5300`。

1. 首次进面板会要求**设置管理员邮箱和密码**，务必记牢（这是你以后登录后台的凭证）。

### 方式 B：Docker 部署（可选，适合想长期挂服务器的人）

```bash
git clone https://github.com/langbot-app/LangBot
cd LangBot/docker
docker compose up -d
```

访问 `http://127.0.0.1:5300`，同样首次要设邮箱密码。

> 保持 LangBot 运行的这个终端/容器也**不要关**。

---

## 5. 在 LangBot 里接入 DeepSeek 模型

进入 LangBot 面板 `http://127.0.0.1:5300`：

1. 左侧找到「**模型 / 对话模型**」（Settings → Model Providers 附近）。
2. 新建模型提供商，类型选「**OpenAI 兼容**（OpenAI Compatible）」，填：

| 配置项             | 填什么                           |
| --------------- | ----------------------------- |
| 名称              | `DeepSeek`（随意）                |
| Base URL / 接口地址 | `https://api.deepseek.com/v1` |
| API Key         | 第 2 步申请的 `sk-xxxx`            |

1. 添加模型，模型名填 `deepseek-chat`（需要推理再额外加 `deepseek-reasoner`）。
2. 把 `deepseek-chat` **设为默认模型**，保存。

**验证模型通没通**：面板里一般有「测试/发送一条消息」的按钮，发句「你好」能正常回话就说明 DeepSeek 接好了。

---

## 6. 连接 NapCat（添加 QQ/OneBot 渠道）

在 LangBot 面板：

1. 左侧「**平台 / 渠道**」（Bots / Channels）→ 新建渠道。
2. 类型选「**QQ**」，协议选「**OneBot 11**」。
3. 连接方式选**正向 WebSocket**（LangBot 作为客户端去连 NapCat），填：

| 配置项          | 填什么                       |
| ------------ | ------------------------- |
| WebSocket 地址 | `ws://127.0.0.1:3001`     |
| Access Token | 与 NapCat 第 3.4 步一致（没设就留空） |

1. 保存。渠道状态应显示「**已连接 / connected**」。

**触发策略（重要，防止刷屏）**：在群/会话配置里，把回复方式设成「**仅被 @ 时回复**」（或"必须 @ 机器人"）。这样群里闲聊时机器人不插嘴，只有 @ 它才说话。

---

## 7. 测试（三步确认跑通）

1. **私聊测试**：用另一个 QQ 号私聊你的机器人小号，发「你好」，应收到 AI 回复。
2. **拉小群测试**：把机器人号拉进一个 2~3 人的测试群，@机器人发问题，看是否回复。
3. **验证多轮记忆**：连续问两句相关的话，看它是否记得上文（LangBot 默认带会话上下文）。

跑通后，再把机器人拉进目标群，@ 它即可对话。

---

## 8. 常见问题排查

| 现象                | 原因 / 解决                                                                 |
| ----------------- | ----------------------------------------------------------------------- |
| LangBot 渠道显示"未连接" | ① NapCat 没启动或 3001 端口没监听；② 地址填错（应是正向 `ws://127.0.0.1:3001`）；③ token 不一致 |
| NapCat 里没有消息进来    | 消息格式不是 `array`，回 3.4 步改成 array 后重启                                      |
| 机器人不回话            | ① LangBot 没配默认模型；② DeepSeek key 失效或没余额；③ 触发策略设成"仅@"但你没 @                |
| 回复乱码/解析错          | NapCat 消息格式问题，改成 `array`                                                |
| 二维码登录被风控          | 换 IP、等一段时间；新号先养几天再挂                                                     |
| 电脑一关就掉线           | 正常，本方案要求机器长期开机；要 7×24 稳定建议部署到云服务器（VPS）                                  |

---

## 9. 稳定性与合规建议

1. **别用主号**：非官方协议随时可能被风控，用可弃的小号。
2. **减少同时登录**：NapCat 运行时，不要再用桌面 QQ 登录同一个号。
3. **养号**：新号先挂几天、加点正常好友/群活跃一下，再挂机器人。
4. **频率控制**：LangBot 里设置回复限频、仅 @ 触发，避免短时间大量发言触发风控。
5. **要真正 7×24 稳定**：Windows 本地关机会掉线，建议后续迁移到一台 2C2G 的 Linux VPS 上跑 Docker（NapCat 用 Docker 版，LangBot 用 Docker Compose）。
6. **长期合规**：如果后面要正式对外运营，转用 QQ 官方机器人平台（bot.qq.com，2026 年已支持个人接入 + DeepSeek 原生插件），无封号风险。

---

## 附：一步到位的端口 / 地址速查

| 用途                  | 地址                             |
| ------------------- | ------------------------------ |
| NapCat 管理界面         | <http://127.0.0.1:6099/webui/> |
| NapCat ↔ LangBot 通信 | ws://127.0.0.1:2280/ws         |
| LangBot 管理后台        | <http://127.0.0.1:5300>        |
| DeepSeek API        | <https://api.deepseek.com/v1>  |
