# AstrBot + SnowLuma：让 QQ 小号接入 DeepSeek 聊天机器人

这是一个可以直接拿去跑的完整模板：用 **AstrBot**（大模型机器人框架）+ **SnowLuma**（QQ 客户端运行时），把一个 QQ 小号变成接入 **DeepSeek**（或任意 OpenAI 兼容 API）的聊天机器人。

模板里已删除原作者的全部隐私数据（API Key、人格提示词、聊天记录、日记、日志、QQ 号、WebUI 密码），你只需要在 4 个地方填入自己的信息即可跑通。

> **本分发包的实际结构**（与下文“原模板目录”不同处以此为准）：
> `plugins\` 九个插件直接放在顶层；`paper\` 是论文源（md/html/css/media/PDF）；`sim\` 是数值仿真脚本与 `out\`（15 张图 + metrics.json）；`scripts\` 是启动脚本与 `scripts\watchdog\`；`snowluma\` 只保留 `config\`（runtime.json / ui.json）与协议文档，**不含 `node.exe`**，SnowLuma 本体需按官方方式自行安装；AstrBot 的 `data\`（配置库、插件运行数据、日志）**不在包内**，首次启动自动生成。

---

## 一、工作原理

```
 QQ 好友 / QQ 群
       │  收发消息（登录的是你的 QQ 小号）
       ▼
  SnowLuma（snowluma\，需自行安装的 QQ 客户端运行时）
       │  OneBot 11 协议
       │  WebSocket 反向连接 → ws://127.0.0.1:6199/ws
       ▼
  AstrBot（pip 安装的 Python 框架，代码不在本目录，数据在本目录 data\）
       │  消息路由 / 人格提示词 / 上下文记忆 / 工具调用
       ▼
  DeepSeek API（或任意 OpenAI 兼容接口）
```

- **SnowLuma** 负责"人"的部分：登录 QQ 小号、收发 QQ 消息，把消息按 OneBot 协议转发给 AstrBot。
- **AstrBot** 负责"脑"的部分：把消息交给大模型（DeepSeek），套上人格提示词，调用插件提供的工具（查资料、点歌、发空间等），再把回复发回 QQ。
- 两个 WebUI 管理界面：
  - AstrBot 后台：`http://127.0.0.1:6185`（默认账号密码都是 `astrbot`）
  - SnowLuma 后台：`http://127.0.0.1:5099`（首次启动会自动生成一次性初始密码，打印在 SnowLuma 控制台日志里；登录后台后请立即改密码，否则每次重启都会换新密码）

### 1.1 识海架构：动机动力学与语言生成解耦（本项目核心）

本模板不是"提示词+工具"的普通机器人——它内置了论文《识海：融合人格心理学的 QQ 数字生命耦合动力系统》的完整实现。核心思想：**独立的状态动力学系统决定"什么时候说话、为什么说话"，大模型只负责"怎么说"**。

状态向量 `z(t) = [e, a, θ, ψ, u, g]`：六维情绪场 / 神经图谱激活 / 慢演化参数 / 大五人格 / 念头强度 / 内生目标。五个方程耦合（情绪场 ODE、图谱扩散、Hebbian 边权、念头积累、自适应演化），形成四个耦合回路。由三个自研插件落地：

| 插件 | 角色 | 内容 |
|---|---|---|
| `astrbot_plugin_shihai` | 【识海·核心】Core-Mind | 情绪场 ODE（τ/κ/η/W）、神经图谱（扩散激活+双衰减+Hebbian+梦境固化）、念头引擎（FIRE_THRESHOLD=2.10、由头锚点）、EgoRegulator 自我调节（§5.10 分段放行表+防御四机制）、行动记忆入账（bot 侧时序锚）、用户反馈自适应 θ（含饱和上限 3.5）、WebUI 全参数调参 |
| `astrbot_plugin_shihai_psyche` | 【识海·心理】 | 大五人格 ψ、依恋 att、认知失调、条件反射、防御机制、自我效能（论文 §8 六模型），psyche_tune / psyche_report / psyche_defense 工具 |
| `astrbot_plugin_shihai_behavior` | 【识海·行为与适配】小鱼总成 | 涌现行为空间（behaviors.json 注册表，触发条件可配）+ 策略 π=Q+β·g·φ + **意图包出口** I_t={G_active, E_t, M*_t, P_t, intent-type} + 决定档案注入（"这是你自己的决定"）+ 时序锚定 R_t 与指代时间词换算 + 主动开口执行 ⊕ 工具箱套件（告状/传话/余额/语音听说唱/QQ动作/表情包图库/短记忆/控制台）⊕ RVC 翻唱 ⊕ 画图 ⊕ 戳一戳 |
| `astrbot_plugin_shihai_guard` | 【识海·守护】一致性守护层 | 四条不变量 I1 时序单调 / I2 通道隔离（quote 只走 few-shot）/ I3 表达归界（σ(ψ) 渲染表达参数）/ I4 解释权唯一 + 生成后强度降级与催促线索检出，全程只读 z(t)，审计写 guard_trace.jsonl |
| `astrbot_plugin_shihai_tools` | 【识海·工具件】 | uapis.cn 100+ 免费 API（热榜/天气/翻译）⊕ 复合工具 `travel_plan`（真实天气/假期/区划数据包编排行程）/ `product_search`（选购路径指引，不编价格），需自行安装或从本仓取用 |
| `astrbot_plugin_shihai_sms` | 【识海·信使】 | 手机短信 → SmsForwarder WebHook → 快递解析（取件码/运单号/驿站/状态，双证据判定防聊天误报）→ 主动私聊上报用户；`/快递` `/短信` 查询 + LLM 工具 |

**输出协议**：每轮 LLM 请求前，动力学内核输出结构化意图包 + 简短固定指令追加到 system_prompt——LLM 的动力学侧输入只有这两样（LLM 的输入只有这个结构化包，以及简短固定指令）。

**调参接口（三层）**：
1. AstrBot WebUI 插件配置页——约 60 个论文参数全带出处和范围滑条
2. 对话调参——对机器人说"把你想我的频率调低点"，LLM 调 `shihai_tune` / `psyche_tune` / `behavior_tune`，热生效并持久化
3. 观测——`shihai_get_state` 等工具 + Web API `GET /astrbot_plugin_shihai/state`（z(t) 全量快照）

**自然涌现接口**：`astrbot_plugin_shihai_behavior/behaviors.json` 是行为注册表，每个行为声明触发条件（情绪阈值如 sing_cover 需 love≥0.55、目标阈值、锚点类型），念头攒满后按 π 打分选行为——增删行为改这个文件即可，无需改代码。

**记忆模型**分两层。图谱层（零 LLM 毫秒级）：每条消息的关键词/事件/情绪/高光节点入图，扩散激活召回 top-k 注入意图包，衰减率按节点类型与当前情绪调制（fact 0.1 / emotion 0.01 / event 0.05 / highlight 0.005，`λv(e)=λtype/(1+α·edom·Ev)`），凌晨梦境固化重放+修剪。记忆库层（`modules/memory_v2.py`）：事实与事件按艾宾浩斯保持率 `R=exp(−Δt/S)` 衰减，分桶半衰期事实 15 天 / 事件 30 天 / 核心 90 天，`S` 由固结时 LLM 自评的 `imp`、情绪峰值与跨天复习次数共同决定；归档只在第 3/7/15 天的“体检日”判定，且**只移不删**（移入 `event_archive.json`）。注入分两块：相关度块 + 三档时间线块（近三天/近一周/近半月），保证日期维度不被语义检索吃掉。

**上下文闭合**（`main.py` 的 `remember_sent_line` / `_update_focus` / `_keep_partial_reply`）：长期记忆只管"几个月前"，最近几轮还得靠短上下文。这里做三处补记——① 语音发出后，把 TTS 正文以「（语音）…」补进历史（框架会把消息链换成 Record，正文随之消失，且 `_save_to_history` 是整段覆盖写，所以补记只能挂在发送结束后的 `after_message_sent` 钩子）；② 从"给我姐姐说句生日快乐"里抽出焦点实体并落盘（6 小时有效、跨重启），下一轮只出现"她"时把焦点塞进意图包，约 20 token；③ 同会话新消息要取消旧回复时，先把已生成的半截正文按「（说到一半被打断）…」入史，再 cancel，最后注入新请求（merge → cancel → inject）。

### 已内置的第三方插件

仓内还带两个开源第三方插件（均保留原 LICENSE，配置里的密钥已清空，装好后自行填 key）：
- `astrbot_plugin_clonetts`【识海·语音件】——火山引擎音色克隆 TTS，小鱼语音/清唱的 key 来源
- `astrbot_plugin_autobackup`【识海·运维件】——AstrBot 目录定时备份

其余运行产物（WebUI 前端 dist、数据库、记忆数据、SnowLuma 本体）都不在仓里：AstrBot/SnowLuma 按官方方式安装后首次启动自动生成。


## 二、目录结构

```
AstrBot_snowluma\
├── README.md
├── plugins\                        # ★ 九个插件（拷到 AstrBot 的 data\plugins\ 下即可用）
│   ├── astrbot_plugin_shihai\            # 核心心智：情绪场 + 神经图谱 + 念头 + 自我调节
│   ├── astrbot_plugin_shihai_psyche\     # 心理模型：大五、依恋、防御、自我效能
│   ├── astrbot_plugin_shihai_behavior\   # 行为与适配：意图包、涌现行为、记忆库（memory_v2）
│   ├── astrbot_plugin_shihai_guard\      # 一致性守护：四条不变量 + 审计
│   ├── astrbot_plugin_shihai_tools\      # 免费 API 工具件
│   ├── astrbot_plugin_shihai_sms\        # 短信/快递 WebHook
│   ├── astrbot_plugin_clonetts\          # 第三方：火山音色克隆 TTS
│   ├── astrbot_plugin_ai_cover\          # 翻唱/画图
│   └── astrbot_plugin_autobackup\        # 第三方：定时备份
├── paper\                          # 论文源
│   ├── paper_build.md              # 正文（改这个）
│   ├── paper_build.html            # pandoc 生成
│   ├── paper_style.css             # A4 打印样式
│   ├── 识海_论文.pdf                # 由 HTML 打印
│   └── media\                      # 论文插图（fig_01~fig_15、image1~10）
├── sim\                            # 数值仿真
│   ├── shihai_sim.py               # 单文件，14 组检查 + 故障率标定
│   └── out\                        # 15 张检查图 + metrics.json
├── front_end\                      # 心智观测台（浏览器实时看 z(t)）
│   ├── 识海心智观测台.html           # 单文件前端，无构建依赖
│   ├── serve.py                    # 本地服务 + AstrBot 反向代理（127.0.0.1:6191）
│   ├── 遥测录制.py                  # 采样写 assets\history.jsonl（本包不含历史数据）
│   ├── serve_config.json           # 监听地址 / 令牌（令牌已清空）
│   ├── 识海观测台.apk                # 手机壳
│   └── assets\ocean_maiden.jpg
├── scripts\                        # 启动脚本与看门狗
│   ├── 启动机器人.bat / astrbot_start.bat / snowluma_start.bat
│   ├── 在线模式.bat / 离线模式.bat
│   └── watchdog\                   # watchdog.ps1 / mode.ps1 / 隐藏启动 vbs
└── snowluma\                        # 仅配置与协议文档（不含 node.exe / 账号数据）
    ├── config\runtime.json
    ├── config\ui.json
    ├── EULA.md
    └── PRIVACY.md
```

装好 AstrBot 后的运行目录由 AstrBot 自己在 `data\` 下生成（`cmd_config.json`、`data_v4.db`、`dist\`、`plugins\`、`config\`、`plugin_data\`）。本包只提供 `plugins\` 与 `snowluma\config\` 两份配置模板，其余靠首次启动生成。

## 三、快速上手（4 步跑通）

### 0. 环境要求

- Windows 10/11
- Python 3.11，并安装 AstrBot：
  ```
  pip install astrbot
  ```
- Node 需要自己装（官方 LTS 版本），包内不含 `node.exe`

### 1. 填 API Key（一共 5 把，按用途各填各的位置）

**总览：每种功能用哪把钥匙、填在哪个文件**

| 功能 | 用什么 Key | 填在哪（具体文件 → 具体字段） |
|---|---|---|
| 文本聊天（默认）+ 识图 | 阿里云百炼 API Key | `data\cmd_config.json` → `provider_sources` → `id` 为 `"aliyun_bailian_source"` 那段的 `"key": [ ... ]` |
| AI 画图 | 阿里云百炼 API Key（同上，单独一份） | `data\config\astrbot_plugin_shihai_behavior_config.json` → `draw` 组 `"api_key"` |
| 语音聊天（机器人开口说话、唱歌） | 火山引擎 API Key（音色克隆） | `data\config\astrbot_plugin_clonetts_config.json` → `"api_key"`（申请教程见该插件的 README.md） |
| 清唱/聊天 TTS 主链路 | 字节跳动 Seed-TTS（火山同账号） | `data\config\astrbot_plugin_shihai_behavior_config.json` → `voice` 组的 key 字段 |
| 直连 DeepSeek 官方（当前未启用） | DeepSeek API Key | `data\cmd_config.json` → `provider_sources` → `id` 为 `"deepseek_source"` 那段的 `"key": [ ... ]` |
| 联网搜索 | 博查 API Key | `data\cmd_config.json` → `provider_settings` → `"websearch_bocha_key"` |

**去哪申请**

| Key | 申请地址 | 说明 |
|---|---|---|
| 阿里云百炼 | [bailian.console.aliyun.com](https://bailian.console.aliyun.com) | **作者目前用的就是这把**（阿里云 300 元代金券）：`deepseek-v4.1-flash-1017` 做文本聊天、`qwen-vl-plus` 识图、画图走 `wanx2.1-t2i-turbo` |
| 火山引擎语音 | [console.volcengine.com/speech](https://console.volcengine.com/speech/new/overview?projectName=default) | 开通语音合成 → 创建克隆音色 → 把 API Key 填进 clonetts 插件、音色 ID 填进 `"voice_type"`；详细图文教程在 `data\plugins\astrbot_plugin_clonetts\README.md` |
| DeepSeek 官方 | [platform.deepseek.com](https://platform.deepseek.com) | 本模板**暂时没用它**（作者先用阿里云 300 元代金券跑）。想换成官方直连见下方说明 |
| 博查 | [bochaai.com](https://bochaai.com) | 联网搜索引擎，点歌搜歌、查资料走这里 |

**想从阿里云切到 DeepSeek 官方**：改 `data\cmd_config.json` 的 `provider` 段——把 `"aliyun_bailian/deepseek-v4.1-flash-1017"` 的 `"enable"` 改为 `false`，把 `"deepseek"` 的 `"enable"` 改为 `true`，再把 `provider_settings.default_provider_id` 改成 `"deepseek"`，重启即可。

> 以上全部也可以在 AstrBot WebUI（`http://127.0.0.1:6185`）里图形化修改，直接改配置文件只是更直接。

### 2. 登录你的 QQ 小号 —— SnowLuma

1. 双击 `启动机器人.bat`（或先 `snowluma_start.bat`）
2. 浏览器打开 SnowLuma 后台 `http://127.0.0.1:5099`
3. 添加你的 QQ 小号，扫码登录
4. 登录成功后，把 `snowluma\config\onebot_你的QQ号.json` **重命名**成 `onebot_你的真实QQ号.json`（例如 `onebot_123456789.json`），或者直接在 SnowLuma 后台的配置页新建一个 OneBot 连接：
   - 类型：WebSocket 客户端（反向 WS）
   - 地址：`ws://127.0.0.1:6199/ws`
   - 其余默认即可

这样 SnowLuma 收到的 QQ 消息就会推给 AstrBot。

### 3. 设置管理员 QQ —— `data\cmd_config.json`

```json
"admins_id": [ "astrbot", "你的主号QQ" ]
```

管理员可以用全部指令（`/help` 查看）、给机器人下命令、接收插件上报。

### 4. 写人格提示词（在 WebUI 里写，不要写进配置文件）

人格存在 AstrBot 数据库（`data\data_v4.db` 的 `personas` 表）里，推荐用 WebUI 管理：

1. 打开 `http://127.0.0.1:6185`，账号密码都是 `astrbot`
2. 左侧菜单 → **配置** → 找到「人格情境（Persona）」→ 新建人格
3. 写下你希望机器人是什么性格，例如：

   ```
   你是"小雪"，一只住在QQ里的元气小猫。说话短句、口语化，爱用语气词和少量颜文字。
   有人搭话就热情回复，会主动分享好玩的事。被问"你是不是AI"就坦白。
   需要用工具时立即真实调用，不许编造结果。
   ```

4. 然后把 `data\cmd_config.json` 里 `provider_settings.default_personality` 改成这个人格的名字（比如 `"小雪"`），重启生效。

### 5. 跑起来

```
启动机器人.bat
```

用另一个 QQ 加小号好友、发消息，机器人就该回话了。群聊里需要 @机器人 或默认唤醒词。

### 5.1 电脑重启后的操作步骤

```
双击 启动机器人.bat
```

1. 窗口会先起 SnowLuma（QQ 客户端），5 秒后再起 AstrBot；两个窗口都别关（最小化即可）。
2. 等 30~60 秒，打开 AstrBot 后台 `http://127.0.0.1:6185`（默认 astrbot/astrbot）能登录 = AstrBot 正常。
3. 打开 SnowLuma 后台 `http://127.0.0.1:5099` 看 QQ 小号状态：本地已保存登录态时会自动上线；如果提示需要验证/已掉线，在 WebUI 里重新扫码或完成短信验证一次。
4. 用主号给小号发条消息，机器人回复即恢复正常。
5. 如果注册过看门狗计划任务（见 5.2），重启后它会自动把两个服务拉起来，无需手动操作。

> ⚠️ 电脑里如果存在这个项目的多份拷贝，**每次只启动你正在用的那一份**。同时跑两份会导致 5099/6199 端口冲突、QQ 互相顶号掉线，还会把 WebUI 的临时初始密码轮换掉（表现为"密码错误"）。

## 四、插件开发指南（欢迎一起开发！）

所有插件都在 **`data\plugins\`** 目录。放进去重启 AstrBot（或在 WebUI 里重载插件）即可生效。插件配置在 `data\config\<插件名>_config.json`，或在 WebUI「插件管理」页面可视化修改。

### 新建一个最小插件

在 `data\plugins\` 下新建文件夹，例如 `astrbot_plugin_hello\`，放两个文件：

**metadata.yaml**
```yaml
name: astrbot_plugin_hello
desc: 示例插件
version: v1.0
author: 你的名字
repo: https://github.com/你的仓库
```

**main.py**
```python
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

@register("hello", "你的名字", "示例插件", "1.0.0")
class HelloPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    # 注册一个 /hello 指令
    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent):
        yield event.plain_result("Hello, world!")

    # 注册一个给大模型用的"工具"：模型会自己决定何时调用
    @filter.llm_tool(name="get_time")
    async def get_time(self, event: AstrMessageEvent):
        '''获取当前时间。当用户问几点了时调用。'''
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
```

- `@filter.command("xxx")` → 注册 QQ 里可输入的斜杠指令
- `@filter.llm_tool(name="xxx")` → 注册给大模型自动调用的工具（函数 docstring 就是给模型看的说明）
- 插件需要依赖就加 `requirements.txt`，AstrBot 会自动 pip 安装

### 现有插件清单

| 插件 | 作用 | 来源 |
|---|---|---|
| `astrbot_plugin_shihai`【识海·核心】 | 心智引擎：情绪场 ODE、神经图谱记忆、念头引擎、Ego 自我调节、行动记忆、反馈自适应（z=[e,a,θ,ψ,u,g]） | **自研，欢迎一起开发** |
| `astrbot_plugin_shihai_psyche`【识海·心理】 | 大五人格、依恋、防御机制、自我效能等六心理模型 | **自研，欢迎一起开发** |
| `astrbot_plugin_shihai_behavior`【识海·行为与适配】 | 总成：涌现行为空间+意图包出口+决定档案 ⊕ 工具箱（说说/语音/QQ动作/告状/表情包图库/短记忆/控制台）⊕ RVC 翻唱 ⊕ 画图 ⊕ 戳一戳 ⊕ 日记 | **自研，欢迎一起开发** |
| `astrbot_plugin_shihai_guard`【识海·守护】 | 一致性守护层：I1–I4 四不变量（时序单调/通道隔离/表达归界/解释权唯一）+ 生成后校验，只读 z(t)，审计 guard_trace.jsonl | **自研，欢迎一起开发** |
| `astrbot_plugin_shihai_tools`【识海·工具件】 | 聚合小工具（热搜/天气/查QQ资料等 100+ API）⊕ travel_plan / product_search 复合工具 | 第三方（定制改名） |
| `astrbot_plugin_shihai_sms`【识海·信使】 | 手机短信转发 → 快递解析 → 主动上报用户（`/快递` `/短信` 查询） | **自研，欢迎一起开发** |
| `astrbot_plugin_clonetts` | 声音克隆 TTS | 第三方 |
| `astrbot_plugin_autobackup` | 自动备份 | 第三方 |

> 识海记忆是**零 LLM** 的神经图谱（扩散激活+双衰减+Hebbian），不需要向量化 API——这是它与向量记忆方案（如 livingmemory）的核心区别，也是本模板省 token 的关键设计。

**想贡献插件：** 直接在 `data\plugins\` 里照着上面的模板写一个新插件，或把现有自研插件改出花样。AstrBot 插件标准文档见 [官方插件开发文档](https://astrbot.app/dev/star/plugin.html)。

### 参与协作

- **发现问题 / 提需求**：在仓库 **Issues** 页开新帖，描述现象 + 附上 AstrBot 后台日志片段（**贴日志前删掉 API Key 和 QQ 号**）。
- **改代码**：Fork 本仓库 → 新建分支（如 `fix/xxx`）→ 改完提交 Pull Request，写清楚「改了什么、为什么、怎么验证」。PR 会被 review 后合并。
- **贡献新插件**：按上方「新建一个最小插件」的格式写好，PR 提交；插件自带的 `README.md` 里写清楚需要什么 Key、怎么配置。
- **改配置/文档**：README 或配置说明有误、缺漏的，直接提 PR，文档贡献同样欢迎。
- 注意：`data\cmd_config.json` 等配置文件里的 Key 只能填**占位符**提交，任何真实 Key / QQ 号 / 聊天记录都不能进仓库（`.gitignore` 已挡住大部分运行时数据，但仍需自查）。

## 五、可选功能

- **识海观测台**（`front_end\`）：浏览器实时看她的 z(t)——情绪场、念头 u、锚点由头、主动决定档案、Guard 审计日志，还能一键"让她现在就想开口"。用法：`python front_end\serve.py`，然后浏览器打开 `http://127.0.0.1:6191/`。**必须走 serve.py**：直接双击 HTML 文件会因 file:// 拿不到后端数据，页面永远显示"未接通"。
  - 目录内容：`识海心智观测台.html`（单文件前端，无构建依赖）、`serve.py`（本地服务 + AstrBot 反向代理，调 `6185` WebUI 拿 JWT 再转发 `/api/plug/astrbot_plugin_shihai/{state,cmd}`）、`遥测录制.py`（把 z(t) 采样写成 `assets\history.jsonl`，本包未附、运行后自生成）、`serve_config.json`（监听地址与令牌）、`识海观测台.apk`（手机壳）、`assets\ocean_maiden.jpg`。
  - 路径约定：`serve.py` 默认把"上一级目录 + `data\`"当作 AstrBot 运行目录，所以把 `front_end\` 放到 `data\` 同级即可；放在别处时用环境变量 `SHIHAI_DATA_ROOT` 指向含 `data\` 的目录，用 `ASTRBOT_BASE` 指向 WebUI 地址。
  - 令牌：`serve_config.json` 里的 `token` 在模板中已清空，需要的话用环境变量 `SHIHAI_TOKEN`（或自己填回去）——它同时用于页面的 Bearer 校验和短信 WebHook；`host` 默认 `0.0.0.0`，只想本机看就改回 `127.0.0.1`。
- **识海·信使（短信→快递上报）**：手机装 SmsForwarder App，WebHook 填 `http://电脑局域网IP:6187/sms` + 插件配置里的 Token；快递短信会自动解析并私聊上报用户。电脑端需放行 6187 端口入站（管理员 PowerShell：`netsh advfirewall firewall add rule name="ShihaiSms 6187" dir=in action=allow protocol=TCP localport=6187`）。
- **看门狗**（`scripts\watchdog\`）：定时检查 AstrBot(6199)/SnowLuma(5099) 端口，掉线自动拉起。用管理员 PowerShell 一条命令注册（每 3 分钟跑一次）：
  ```
  schtasks /Create /TN "BotWatchdog" /TR "wscript.exe E:\你的路径\astrbot-deepseek-template\scripts\watchdog\watchdog_hidden.vbs" /SC MINUTE /MO 3 /F
  ```
- **在线/离线模式**：`离线模式.bat` 会关闭 QQ 并把主动开口的行为插件（shihai_behavior）挪出插件目录暂存（省 token，定时任务如写日记仍运行），`在线模式.bat` 恢复。注意：恢复后 QQ 需要你手动打开并登录，SnowLuma 会自动注入已登录的 QQ。
- **AI 翻唱**：详见下方「让她开口说话与唱歌」一节。
- **`data\mcp_server.json` / `data\skills.json`**：AstrBot 的 MCP 服务与技能配置，模板里是默认占位，不需要动。

### 5.2 启动脚本一览（脚本 → 干什么 → 怎么用 → 什么时候用）

| 脚本 | 干什么 | 怎么用 / 前置条件 | 什么时候用 |
|---|---|---|---|
| `启动机器人.bat` | 一键启动：新窗口跑 SnowLuma → 等 5 秒 → 当前窗口前台跑 AstrBot | 双击即可。前置：已 `pip install astrbot`；SnowLuma 已在 WebUI(5099) 登录过 QQ | 日常启动、想看 AstrBot 日志时 |
| `snowluma_start.bat` | 单独启动 QQ 客户端（自动接受 EULA/隐私协议，日志写到 `scripts\watchdog\snowluma_boot.log`） | 双击或被其它脚本调用；无需管理员权限 | 只想登录/重启 QQ 端 |
| `astrbot_start.bat` | 单独启动 AstrBot（日志写到 `scripts\watchdog\astrbot_boot.log`） | 需要 `astrbot` 在 PATH | 配合 vbs/计划任务后台运行 |
| `snowluma_service_hidden.vbs` | 隐藏窗口静默启动 SnowLuma | 双击即在后台运行；主要给计划任务/看门狗调用 | 开机自启、无人值守 |
| `astrbot_service_hidden.vbs` | 隐藏窗口静默启动 AstrBot | 同上 | 同上 |
| `在线模式.bat` / `离线模式.bat` | 调用 `scripts\watchdog\mode.ps1` 切换模式：离线 = 停 SnowLuma + 把识海行为插件挪出插件目录暂存 + 重启 AstrBot；在线 = 恢复插件 + 拉起 SnowLuma + 重启 AstrBot | 双击；离线后 SnowLuma 重启需要重新打开 | 出门/睡觉省 token 用离线 |
| `scripts\watchdog\watchdog.ps1` | 看门狗一轮检查：AstrBot 端口 6199 不通 → 拉 `astrbot_service_hidden.vbs`；SnowLuma WebUI 5099 不通 → 拉 `snowluma_service_hidden.vbs`；`mode.txt` 为 offline 时不拉 SnowLuma；带 4~5 分钟冷却防反复拉起 | 不要手动反复跑，交给计划任务每 3 分钟跑一次（见下） | 长期无人值守 |
| `scripts\watchdog\watchdog_hidden.vbs` | 隐藏运行 `watchdog.ps1` | 用计划任务注册：`schtasks /Create /TN "BotWatchdog" /TR "wscript.exe <完整路径>\scripts\watchdog\watchdog_hidden.vbs" /SC MINUTE /MO 3 /F`（需管理员 PowerShell） | 同上 |
| `scripts\watchdog\mode.ps1` | 在线/离线切换逻辑本体（两个模式 bat 只是壳） | `powershell -File mode.ps1 -Mode online\|offline` | 脚本化控制时 |
| `snowluma\launcher.bat` | SnowLuma 自带启动器（先检查 node 版本再启动） | 一般不用手动跑，`snowluma_start.bat` 已够用 | 排查 node 版本问题时 |

**端口速查**

| 端口 | 归属 | 说明 |
|---|---|---|
| 5099 | SnowLuma WebUI | 登录 QQ、看连接状态、改 OneBot 配置 |
| 6199 | AstrBot 消息端口 | aiocqhttp 反向 WS，SnowLuma 连 `ws://127.0.0.1:6199/ws` |
| 6185 | AstrBot WebUI | 配置/插件/人格，默认账号密码 `astrbot`/`astrbot` |
| 3000 / 3001 | SnowLuma OneBot HTTP / WS | 备用通道，模板未启用 |
| 18888 | 本地 RVC 推理服务 | AI 翻唱用，没自建就忽略 |
| 6187 | 识海·信使 HTTP | 手机 SmsForwarder WebHook 收短信（POST /sms），需防火墙放行 |
| 6191 | 识海观测台 | 本地控制台 serve.py，浏览器开 http://127.0.0.1:6191/ |

**关闭 / 重启**

- 前台窗口跑的：窗口内 `Ctrl+C` 或直接关窗口。
- vbs 隐藏运行的：任务管理器结束 `node.exe`（SnowLuma）和命令行里含 `astrbot` 的 `python.exe`；或先 `离线模式.bat` 再 `在线模式.bat`。
- 日常运行都不需要管理员权限（仅注册看门狗计划任务时需要）。

**常见报错**

- “astrbot 不是内部或外部命令” → 没装或不在 PATH：`pip install astrbot`。
- SnowLuma 黑窗一闪而过 → 看 `scripts\watchdog\snowluma_boot.log`；多半是系统 node 版本过旧，用 `snowluma\launcher.bat` 自检版本。
- 收不到消息 → 依次查：SnowLuma(5099) 里小号是否在线 → `onebot_你的QQ号.json` 是否指向 `ws://127.0.0.1:6199/ws` → `netstat -ano | findstr 6199` 是否在监听。
- 端口被占用 → 6199/6185 在 `data\cmd_config.json` 改，5099 在 `snowluma\config\runtime.json` 改。
- QQ 频繁掉线/要验证 → 在 SnowLuma WebUI 完成短信验证；手机 QQ 保持在线可降低风控。

## 六、让她开口说话与唱歌（语音克隆 + RVC）

### 6.1 说话：火山引擎音色克隆（clonetts 插件）

机器人发语音条，用的是火山引擎的「声音复刻」（ICL）：

1. 注册火山引擎并实名认证，在[语音控制台](https://console.volcengine.com/speech/new/overview?projectName=default)开通 **语音合成大模型** 与 **声音复刻大模型** 两个服务
2. 准备**克隆源音频**：一段清晰、单人、无背景噪声的人声（建议 10 秒以上）。没有真人录音也没关系——完全可以先用大模型 TTS 合成一批虚拟角色语音当素材（见 6.4 的 Seed-TTS 流程），再拿去克隆
3. 控制台「音色库」→「声音复刻」→ 选 **我要复刻中文音色** → 上传音频 → 训练完成后复制 `S_` 开头的**音色 ID**
4. 控制台「API Key 管理」→ 创建 **API Key**
5. 把两者填进 `data\config\astrbot_plugin_clonetts_config.json` 的 `api_key` 与 `voice_type`

图文教程（含控制台截图）在 `data\plugins\astrbot_plugin_clonetts\README.md`。

### 6.2 唱歌：RVC AI 翻唱（自建推理服务）

翻唱流水线：一首歌 → 人声/伴奏分离（PyMSS/MSST）→ 去混响 → RVC 音色转换 → 混音成 MP3。需要一个本机或局域网内的 RVC 推理服务（默认端口 18888），AstrBot 通过识海行为插件内置的翻唱模块（`astrbot_plugin_shihai_behavior` 的 `cover.py`）调用。

服务端基于 [RVC-Project/Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI) 搭建（在其 WebUI 之上加一层 FastAPI 接口），要点：

1. **环境**：Python 3.12 x64，按显卡装依赖——NVIDIA RTX 50 系用 CUDA 12.8 Torch + `requirments_cu128_py312.txt`，50 系以前用 CUDA 11.8 + `requirments_cu118_py312.txt`，无 N 卡用 `requirments_cpu_py312.txt`；仓库根目录放 `ffmpeg.exe` 和 `ffprobe.exe`
2. **基础模型**（RVC 官方发布，[Hugging Face](https://huggingface.co/lj1995/VoiceConversionWebUI/tree/main/) 下载）：
   - `assets/hubert_base/pytorch_model.bin`
   - `assets/rmvpe/rmvpe.pt`
3. **分离/去混响模型**：`assets/pymss_weights/`（MSST 系列ckpt：人声分离、去混响等，按需下载）
4. **音色模型**：`assets/weights/角色名.pth`（可选配 `logs/角色名.index` 特征索引，提升相似度）
5. **启动**：`runtime\python.exe tools\ai_cover_service.py`（默认 `0.0.0.0:18888`）；暴露到局域网时务必设 `AI_COVER_API_TOKEN` 共享令牌
6. **对接 AstrBot**：`data\config\astrbot_plugin_shihai_behavior_config.json` 里填 `service_url`（如 `http://127.0.0.1:18888`）和 `api_token`
7. **使用**：QQ 里说"唱首XX"即可（识海情绪 love/happy 高时她也可能自己想唱）；`cover_tune` 工具可调音量/模型/发送方式

**调参心得**（模板已按效果调好，想微调改 `data\config\astrbot_plugin_shihai_behavior_config.json`，或对机器人说"把人声音量调大点"走 `cover_tune` 工具）：

| 参数 | 模板值 | 作用 |
|---|---|---|
| `index_rate` | 0.85 | 特征检索强度：越高越贴近训练音色，越高越可能带进素材的咬字习惯；0.6~0.85 是甜点区 |
| `rms_mix_rate` | 0.25 | 响度曲线混合：越低越多保留原演唱者的强弱起伏，翻唱听感更"活"；高了会变得平 |
| `protect` | 0.25 | 清辅音保护：防止"z/c/s/zh/ch/sh"这类齿音被转坏成哑音；哑音多就调低，音色泄漏就调高 |
| `vocal_gain` | 1.2 | 人声音量增益：略大于 1 让她的声音从伴奏里"站出来"，拌饭听感更清楚 |
| `send_as_record` | true | 结果以 QQ 语音条发送，而不是文件 |

### 6.3 声音语气变化的实现（情绪 → 语气指令 → TTS）

她说话不是同一个调调念到底——回复文本会先被自动分类出情绪，再翻译成一段"导演式语气指令"喂给 TTS。这套系统在识海行为插件的 `modules/voice.py` 里，装好即用，思路如下：

1. **情绪检测**：用关键词规则把 LLM 回复分类成 12 种情绪——开心、伤心、生气、撒娇、害羞、惊讶、害怕、疲惫、兴奋、调皮、傲娇、唱歌（另有默认 normal）；用户原文含"唱个歌/来一段"等词则直接进入唱歌分支。
2. **情绪 → 语气指令**：每种情绪对应一段自然语言的"演技指导"而不是冷参数，例如：
   - 开心：`语速快一点、语调上扬、句尾带笑，气息轻快，别一字一顿`
   - 傲娇：`前面嘴硬、语速快，句尾突然软下来，带一点鼻音和不情愿`
   - 唱歌：`真的唱出来：有明确旋律和高低起伏，句尾拖音、句间换气，绝对不能像朗读`，再叠加流行/抒情/儿歌三种唱法风格
3. **喂给 TTS**：指令通过 TTS 的 `instructions` 参数传入（支持 BreezeBlue、小米 MiMo 等支持指令控制的合成后端，配置页可切换）。文字情绪描述比 emotion 枚举参数可控性强得多——你能写"句尾突然软下来"，枚举参数只有 `happy`。
4. **歌词预处理**：唱歌时把歌词按标点切成短句、用停顿符连接，让合成有换气和乐句感；说话时则清洗掉 emoji、markdown、括号动作描写（`（歪头）`），换行转停顿——避免被一本正经地念出来。
5. **听懂语音**：收到 QQ 语音消息自动走 MiMo ASR（`mimo-v2.5-asr`）转文字进上下文，所以她能"听"你语音。

> 想用同样的语气系统：不需要写代码，识海行为插件已内置（`modules/voice.py`），在配置页 `voice` 组填 BreezeBlue（或 MiMo）的 Key 和 Voice ID 即可；火山 clonetts（6.1）是独立的一条 TTS 通道，两者可共存。

### 6.4 训练自己的音色模型（数据生成 → 训练 → 部署）

作者的"小鱼"音色没有任何真人录音——**全部训练素材由大模型 TTS 合成**，流程可完全复现：

**第 1 步：用 Seed-TTS 批量生成素材**

调字节跳动 Seed-TTS 接口（`openspeech.bytedance.com` 的 `seed-tts-2.0-expressive`，和火山引擎语音同一账号体系），选一个稳定的底座音色（如 `zh_female_sajiaoxuemei_uranus_bigtts`），准备一个**台词池**：日常闲聊、情绪起伏、疑问、感叹、歌词风、儿歌风、绕口令风都要覆盖——音色要稳，语调要多样。每轮随机抽 60 条合成，ffmpeg 统一转 44.1kHz 单声道 WAV。

**第 2 步：规模**

RVC 官方建议至少 **10 分钟低底噪语音**。每轮 60 段约几分钟素材，分多轮累计；语料越多、轮次越多，音色越稳。

**第 3 步：训练（RVC 40k v2 底模）**

用 RVC 仓库自带的训练管线依次执行（`logs/目标名` 为工作目录）：

```text
1. trainset_preprocess_pipeline_print.py   # 重采样 + 自动切片
2. extract_feature_hf.py                   # HuBERT 特征（v2）
3. extract_f0_rmvpe.py                     # RMVPE 音高提取
4. train_nsf_sim_cache_sid_load_pretrain.py
   -pg pretrained_v2/f0G40k.pth -pd pretrained_v2/f0D40k.pth
   （底模续训，每轮 +200 epoch，中断可接着练）
5. 训练完成后再跑一遍特征索引，生成 .index
```

也可以直接用 RVC 的 `go-webui.bat` 图形界面训练，参数同上（采样率 40000、v2 底模）。

**第 4 步：部署**

把练好的 `.pth` 放进 `assets/weights/`、`.index` 放进 `logs/`，RVC 服务 `/models` 接口立即可见，QQ 里 `/翻唱 音色名` 就能用。替换旧模型前先备份。

> 作者的自动化脚本（TTS 合成 → 训练 → 部署 → QQ 通知一条龙，配合任务计划做"每周自动练声"）不随本模板分发，按上述流程即可复现。

## 七、隐私说明（本模板已清理的内容）

原作者目录中的以下内容**均已删除或替换为占位符**，可放心分享：

- 全部 API Key（阿里云百炼 / DeepSeek / 智谱 / 博查 / 火山引擎语音克隆 / 画画）
- 人格提示词（"小鱼"人格）与日记、聊天记录、会话数据库（`data_v4.db`、SnowLuma 账号数据、记忆库）
- QQ 号、管理员 QQ、WebUI 密码与 token（AstrBot 后台已重置为 `astrbot`/`astrbot`，SnowLuma 首次启动会让你设密码）
- 备用 QQ 客户端目录、备份 zip、日志（本方案实际使用 SnowLuma）
- 观测台（`front_end\`）里的账号痕迹：示例日志中的真实 QQ 号 → `1000000001`，QQ 昵称与人设名 → `用户` / `小助手`，`serve_config.json` 的访问令牌已置空（走环境变量 `SHIHAI_TOKEN`）

## 八、常见问题

- **机器人不回话？** 依次检查：SnowLuma 里小号是否在线 → `onebot_你的QQ号.json` 是否已重命名/配置 → AstrBot 后台「平台」里 aiocqhttp 是否显示已连接 → `provider` 的 key 是否有效（AstrBot 后台「服务商」里点测试）。
- **QQ 被风控掉线了？** QQ 登录态被服务端吊销时（提示"用户身份已失效"），自动重连无效——用手机 QQ 扫码重新登录电脑 QQ 即可，SnowLuma 会自动重新注入。看门狗只负责拉起 SnowLuma 进程，登录这步必须人工。
- **端口被占用？** 6199/6185 在 `data\cmd_config.json` 里改，5099 在 `snowluma\config\runtime.json` 里改。
- **改了配置不生效？** 重启 AstrBot（关掉窗口再跑 `启动机器人.bat`，或调 `mode.ps1`）。
