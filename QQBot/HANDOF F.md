# 识海 QQ 官方机器人简化 Demo 交接文档

核对日期：2026-11-11（Asia/Shanghai）。文件名沿用用户指定的 `HANDOF F.md`。

本文件依据当前对话、实际源码、Git 本地状态、此前测试工具输出和用户提供的真实 QQ 验收截图编写。本轮仅进行只读核对并新增本文件，没有继续开发、重新部署、重启机器人、重跑业务测试、更新源码包或执行 Git 提交、推送。

## 1. 任务目标

用户参加第二十届全国大学生软件创新大赛，是四人团队中的 QQ 端负责人，使用 GitHub Desktop 协作。队长交付的本阶段任务是：

> 接通 QQ 机器人，含心理动力学系统代码的简化 demo，能对话即可。

当前落地方案是 QQ 官方机器人 + AstrBot + DeepSeek 官方 API，将已有识海心智代码接入文字对话。源码交付不包含 SnowLuma。此前对话另有“个人 QQ 小号用于演示视频”的安排，该接入方式未在本 Demo 中实现。

本交接范围是已完成的简化文字对话 Demo，不代表整个识海项目或比赛作品已经完成。

## 2. 工作位置与 Git 状态

| 项目 | 已核对的状态 |
| --- | --- |
| 工作区 | `D:\比赛\软创赛` |
| 实际工作仓库 | `D:\比赛\软创赛\astrbot-deepseek-template` |
| QQ Demo 源码 | `D:\比赛\软创赛\astrbot-deepseek-template\QQBot` |
| AstrBot 运行目录 | `D:\比赛\软创赛\qqbot_runtime` |
| 远程 origin | https://github.com/Zjx20252082383/astrbot-deepseek-template.git |
| 当前分支 | `branch-QQbot` |
| 跟踪分支 | `origin/branch-QQbot` |
| 当前 HEAD | `32ee07f665a3d436712b98236202d86f6ef249f7` |
| HEAD 提交说明 | `front_end/paper: 修正 4 个中文文件名（同一批打包转码问题：观测台 html/apk、遥测录制.py、识海论文 pdf）` |
| 与本地缓存的上游比较 | ahead 0、behind 0；本轮没有 fetch，不代表实时 GitHub 状态 |
| 生成文档前的状态 | `?? QQBot/`；该目录有 19 个未跟踪文件 |
| 已跟踪文件的变更 | `git diff --stat` 与 `git diff --cached --stat` 均无输出 |
| 提交及推送 | 本次协作尚未提交或推送 QQ Demo，未修改 main 分支 |

工作区同时存在 `astrbot-deepseek-template-new` 和 `astrbot-deepseek-template-old` 目录，本次工作的目标是上表中的实际工作仓库。

## 3. 已完成工作

1. 建立独立的 `QQBot` 源码目录，新增 AstrBot 插件 `astrbot_plugin_shihai_demo`，版本 `0.1.0`。
2. 复用原有心智动力学、图谱记忆、意图包和人格表达映射代码；本轮逐字节核对了 12 个复用文件，与仓库原文件一致。
3. 接入模型请求前的状态更新与系统提示注入，继续使用 AstrBot 中已有的模型配置。
4. 实现按“平台会话 + 发送者”隔离的状态目录、JSON 持久化、请求和事件重复处理防护。
5. 提供 `/心境`，展示六维情绪、OCEAN 人格参数、图谱节点/边数量、本次启动后的注入标记和主动外发状态。
6. 启动每 60 秒运行一次的心跳，正常退出时保存状态和图谱；主动外发关闭。
7. 编写部署脚本、说明、10 项单元测试和独立 AstrBot 加载冒烟测试。
8. 插件已部署到实际运行目录；本轮核对 15 个插件源码文件与部署副本逐字节一致。
9. 用户已经完成真实 QQ 文字对话、重启恢复和新建模型会话后的偏好召回验收，并提供聊天和终端截图。
10. 已生成独立精简源码包，真实 QQ 验收结果已记录在 [QQBot 说明](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/README.md) 中。

## 4. 关键决策与实现边界

- **采用官方接入。** 当前平台实例 ID 固定为 `qqbot_deepseek`，真实验收使用 `qq_official`。该实例 ID 与插件 `main.py` 的 `PLATFORM_ID` 一致。
- **复用现有模型配置。** 插件不直接请求 DeepSeek API，也不在源码中存放 API Key；它向 AstrBot 的 `ProviderRequest.system_prompt` 追加意图包、表达参数和约束。
- **保留原有模块。** 本阶段以复制已有模块、增加独立适配入口的方式实现，未修改原有 `plugins` 下的对应文件。
- **隔离用户状态。** 使用 `unified_msg_origin` 与发送者 ID 拼接后计算 SHA-256，前 24 个十六进制字符作为目录名；不同平台会话或不同发送者不会自动合并记忆。
- **区分模型会话和心智状态。** 本次真实样例中 `/new` 建立了新模型会话，但未清除该用户的插件状态，随后仍回忆出“蓝莓”。
- **保持简化范围。** 主动消息、个人 QQ 小号接入、多媒体回复、生活任务和完整守护层未在本 Demo 中实现。只复用了守护模块中的人格表达映射，没有启用完整守护插件。
- **人格使用明确的默认参数。** `O=0.55, C=0.45, E=0.60, A=0.70, N=0.35`；未对长期人格漂移进行验收。
- **保留诊断语义。** “本次启动后已注入”是内存标记，重启后先显示“否”；它不表示磁盘记忆已经丢失，也不因为执行 `/心境` 就变为“是”。
- **控制交付范围。** 独立源码包只打包 QQ Demo，不打包整个仓库和运行目录。排除 SnowLuma 的打包检查已经完成；本轮没有进行赛事规则合规复核。

## 5. 新增、部署及打包的文件

### 5.1 仓库内新增文件

以下 19 个文件在生成本交接文档前均属于未跟踪文件，不是对已跟踪文件的修改。

| 文件或目录 | 内容 |
| --- | --- |
| [main.py](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/main.py) | 新增插件入口：平台筛选、身份隔离、请求注入、心跳、持久化退出和诊断指令 |
| [metadata.yaml](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/metadata.yaml) | 插件名称、版本、框架版本范围和支持平台声明 |
| [requirements.txt](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/requirements.txt) | 依赖声明 `jieba>=0.42,<2` |
| [mind 目录](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/mind/) | 复用 10 个文件：`__init__.py`、`adapt.py`、`classify.py`、`ego.py`、`emotion.py`、`engine.py`、`goals.py`、`graph.py`、`state.py`、`urge.py` |
| [intent.py](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/intent.py) | 复用意图包组装和格式化 |
| [persona.py](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/astrbot_plugin_shihai_demo/persona.py) | 复用人格到表达参数的映射 |
| [deploy_demo.ps1](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/deploy_demo.ps1) | 部署插件并校验逐文件 SHA-256 |
| [README.md](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/README.md) | 安装、运行、复用边界、测试和真实 QQ 验收记录 |
| [test_demo.py](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/tests/test_demo.py) | 10 项单元测试，使用真实心智模块和框架接口替身 |
| [smoke_load.py](D:/比赛/软创赛/astrbot-deepseek-template/QQBot/tests/smoke_load.py) | 在独立临时运行目录中启动真实 AstrBot 并检查加载 |

本轮只新增本交接文档：[HANDOF F.md](<D:/比赛/软创赛/astrbot-deepseek-template/QQBot/HANDOF F.md>)。加入本文件后，QQBot 目录共有 20 个交付源文件，不计 Python 缓存。

复用来源为本仓库当前 HEAD 下的以下位置，12 个 Python 文件本轮比对一致：

- `D:\比赛\软创赛\astrbot-deepseek-template\plugins\astrbot_plugin_shihai\mind`：10 个文件。
- `D:\比赛\软创赛\astrbot-deepseek-template\plugins\astrbot_plugin_shihai_behavior\behavior\intent.py`。
- `D:\比赛\软创赛\astrbot-deepseek-template\plugins\astrbot_plugin_shihai_guard\guard\persona.py`。

### 5.2 仓库外部署与交付物

- 部署副本：`D:\比赛\软创赛\qqbot_runtime\data\plugins\astrbot_plugin_shihai_demo`，15 个插件源文件。
- 运行数据：`D:\比赛\软创赛\qqbot_runtime\data\plugin_data\astrbot_plugin_shihai_demo\<会话哈希>\`，包含 `mind_state.json` 与 `shihai_graph.json`，不属于交付源码。
- [现有源码包](D:/比赛/软创赛/qqbot_runtime/packages/shihai_qq_demo_0.1.0.zip)：19 个文件，64,629 字节，ZIP 根目录为 `QQBot/`。
- 现有源码包 SHA-256：`9abe49f37d32c82649cd8624576df79bce48237dc43f5036284091354b38412a`。
- 本轮没有重新打包，因此该 ZIP 包含此前更新的 README，但不包含本交接文档。

## 6. 验证结果与证据范围

### 6.1 此前已完成的自动测试

证据来自当前对话中的成功工具输出，并已核对当前测试脚本；本轮没有重新运行这两项测试。

| 检查 | 已有结果 | 覆盖范围 |
| --- | --- | --- |
| 单元测试 | 10 项通过 | 平台筛选、空消息/自身消息忽略、情绪记忆与风格注入、重复防护、群内用户隔离、JSON 保存恢复、状态指令、关闭主动触发、异常隔离、心跳生命周期 |
| 真实框架加载冒烟测试 | `plugin_loaded=true`，HTTP 200 | AstrBot 4.29.4 在临时目录成功加载插件；该测试配置 QQ 平台数为 0、模型服务数为 0，不替代真实 QQ/API 测试 |

单元测试使用真实的心智代码、jieba 和临时 JSON；AstrBot 事件与请求接口使用替身。冒烟测试启动自有临时子进程，完成后关闭该进程并清理其临时目录。

### 6.2 用户提供的真实 QQ 验收

| 验收项 | 截图和日志中的实际观察 | 结论 |
| --- | --- | --- |
| 普通文字对话与注入 | 开心消息得到正常回复；随后 `/心境` 显示开心 0.139、惊讶 0.138，图谱 8 节点/40 条边，已注入为“是”；有状态注入日志 | 本次样例通过 |
| 重启恢复 | 新一次启动后的 `/心境` 显示开心 0.165、惊讶 0.167，仍有 8 节点/40 条边，启动后已注入为“否” | 本次恢复验收通过；注入标记符合重启后重置的设计 |
| 新模型会话中的偏好召回 | 告知“我最喜欢的水果是蓝莓”，执行 `/new` 并收到新会话确认，再问“我最喜欢什么水果”，正确回答蓝莓；两次普通对话均有注入日志，节点数先后为 12、14 | 本次召回样例通过 |

截图由用户在 QQ 中操作后提供，助手没有代发这些 QQ 消息。原始截图未复制到交付目录，文档不记录截图中的用户 OpenID、会话哈希或网络地址。

### 6.3 本轮重新执行的只读核对

- 已读取插件入口、元数据、依赖、部署脚本、测试脚本和 README，并核对 Git 状态。
- 实际解释器版本为 Python 3.12.16，环境中的 AstrBot 为 4.29.4、jieba 为 0.42.1。
- 15 个插件源文件与部署副本内容一致。
- 12 个复用文件与原仓库相应文件内容一致。
- 15 个 Python 文件通过内存中的 AST 语法解析，未生成编译缓存。
- 现有 ZIP 的 CRC 检查通过；19 个条目及文件内容与新增本交接文档前的 QQBot 源码完全一致。
- ZIP 中没有 `snowluma`、`data`、`.git`、`__pycache__` 路径，也没有 `cmd_config.json`、`mind_state.json`、`shihai_graph.json`、`sms_records.json` 文件。

## 7. 未解决事项与未验证范围

1. QQBot 源码尚未被 Git 跟踪、提交或推送；当前成果仍在本机工作树中。
2. 现有源码包不包含本轮新增的交接文档；本轮按用户要求只生成文档，没有更新归档。
3. 本次偏好召回仅验证“蓝莓”这一条样例，不能据此认定任意内容都能长期准确召回。
4. 已观察到情绪数值变化和请求状态注入，但没有单独验证它们对模型回复语气的因果影响。
5. 未在真实 QQ 环境验收群聊多用户隔离；该项已有单元测试通过记录。
6. 实际验证环境是 Python 3.12.16 + AstrBot 4.29.4。用户此前报告安装了 Python 3.10，但本任务未在 3.10 下验证；元数据声明的其他 AstrBot 版本和 `qq_official_webhook` 也未实际验收。
7. 个人 QQ 小号演示接入尚未实现；完整生活任务、主动行为、移动端集成和完整守护层不在本阶段已完成内容中。
8. 本轮未检查实时 GitHub 分支状态、推送认证、当前后台是否仍在线，也未复核赛事规则合规性。

当前对话及上述检查中，没有已报告且仍待修复的本阶段运行错误；这不是对未测试场景的保证。

## 8. 下一步操作

以下是交接后的操作清单，本轮未执行。

1. 阅读本文件与 QQBot README，确认交接范围为官方 QQ 文字对话 Demo。
2. 在 GitHub Desktop 选择 `D:\比赛\软创赛\astrbot-deepseek-template`，确认分支为 `branch-QQbot`，逐项检查 QQBot 目录的新增文件，包括本交接文档。
3. 只选择本次 QQBot 源码和文档进行提交。可使用提交说明：`feat: 接入QQ官方机器人的识海简化对话Demo`，随后点击 `Push origin` 推送当前分支。
4. 按仓库 README 的团队流程，由集成者审核相关 PR 后合并；本轮未创建 PR，也未执行仓库级合并前检查。
5. 如果后续要求 ZIP 一并包含本交接文档，再单独更新源码包；当前包应按本文件记录的 19 个文件版本识别。

## 9. 运行与复核命令

以下命令供接手者使用，不表示本轮已执行部署或测试。PowerShell 中明确使用已验证的解释器和运行目录，避免混用其他 Python 环境。

### 9.1 查看本地 Git 状态

```powershell
Set-Location -LiteralPath 'D:\比赛\软创赛\astrbot-deepseek-template'
git status --short --branch
git log -1 --oneline
```

### 9.2 部署插件

```powershell
& 'D:\比赛\软创赛\astrbot-deepseek-template\QQBot\deploy_demo.ps1' -RuntimeRoot 'D:\比赛\软创赛\qqbot_runtime'
```

脚本要求目标目录已初始化，即存在 `.astrbot`，并复制、校验该插件的源文件。部署后还需要在 AstrBot 中重载插件或重启服务；脚本本身不执行重载、重启。

### 9.3 启动实际机器人

```powershell
Set-Location -LiteralPath 'D:\比赛\软创赛\qqbot_runtime'
& 'C:\Users\Lenovo\.local\bin\astrbot.exe' run
```

如果原有进程仍在运行，先在其原启动终端按 Ctrl+C 正常退出，再启动，避免重复运行。真实实例已配置 QQ 官方机器人和 DeepSeek，凭据保留在运行配置中。

### 9.4 运行已有测试

```powershell
$shihaiPython = 'C:\Users\Lenovo\AppData\Roaming\uv\tools\astrbot\Scripts\python.exe'
Set-Location -LiteralPath 'D:\比赛\软创赛\astrbot-deepseek-template'
& $shihaiPython -B -m unittest discover -s 'D:\比赛\软创赛\astrbot-deepseek-template\QQBot\tests' -v
& $shihaiPython -B 'D:\比赛\软创赛\astrbot-deepseek-template\QQBot\tests\smoke_load.py'
```

单元测试不调用模型 API；加载冒烟测试不配置真实 QQ 或模型，需要允许创建本机临时监听端口和子进程。

### 9.5 QQ 中的人工复核指令

按顺序手动发送以下内容：

```text
/心境
今天特别开心，事情终于做好了，太棒了！
/心境
我最喜欢的水果是蓝莓
/new
我最喜欢什么水果
```

重启恢复的复核方式：正常停止并重新启动 AstrBot，然后先发送 `/心境`，查看保存的情绪与图谱；启动后的注入标记应等下一条普通对话才变为“是”。

## 10. 环境要求与注意事项

| 项目 | 本机已确认状态或要求 |
| --- | --- |
| 操作系统与终端 | Windows，PowerShell |
| Python | 实际验证为 3.12.16，路径见测试命令 |
| AstrBot | 实际安装并验证为 4.29.4；插件元数据声明 `>=4.29.4,<5` |
| jieba | 实际安装版本 0.42.1；插件依赖声明 `>=0.42,<2` |
| QQ 接入 | 官方机器人；实例 ID `qqbot_deepseek` |
| 对话模型 | 用户配置的 DeepSeek 官方 API，由 AstrBot 提供，插件复用 |
| 文件权限 | 运行目录需要允许写入插件状态 JSON |
| 网络 | 实际机器人对话需要访问 QQ 与模型服务；独立冒烟测试检查本机 HTTP |

- 运行配置、API Key、登录信息、聊天记录及插件状态不属于交付源码，不要把整个运行目录加入 Git。
- 当前工作仓库本身仍有原来的 SnowLuma 相关目录；应使用已核对的独立 QQ Demo 包交付，不能把“当前仓库整体”与“精简源码包”混为一谈。
- 不将 `astrbot-deepseek-template-old`、`astrbot-deepseek-template-new` 或运行目录当成本次工作仓库，不使用强制添加方式提交被忽略的运行数据。
- 更换平台实例 ID 时，当前代码中的 `PLATFORM_ID` 必须与 AstrBot 实例一致；跨平台、跨会话身份不会自动合并。
- 修改源码后需要重新部署并重载或重启，Git 提交不会自动更新实际运行副本。
- 本轮仅新增本交接文档；此前测试通过和 QQ 验收结论均按实际证据范围记录，没有扩展到未验证功能。
