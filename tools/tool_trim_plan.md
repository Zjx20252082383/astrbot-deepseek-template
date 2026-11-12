# 工具 schema 瘦身清单

生成日期：2026-11-11
配套文件：`tool_trim_plan.json`（结构化，供 `tool_trim_apply.py` 读取）

---

## 一、结论

每次请求发送 **75 个工具定义、36,098 字符**，占单次输入（约 72,000 字符 / 43,870 token）的 **49.9%**。

其中 **enum（参数取值枚举）合计 11,688 字符，占全部 API schema 的 28.5%**；而仅 `misc.worldtime` 一个工具就吞掉 **8,080 字符**——它一次都没被调用过。

本清单可直接执行的部分（A + B）合计省 **10,709 字符 ≈ 6,693 token ≈ 15.3% 的账单**，且不损失任何实际能力。

---

## 二、数据基线

| 指标 | 值 | 来源 |
|---|---|---|
| API 定义总数 | 99 | `astrbot_plugin_shihai_tools/api_registry.py` |
| API schema 合计 | 40,990 字符 | 同上 |
| enum 合计 | 11,688 字符（28.5%） | 同上 |
| 每请求注册工具数 | 75 | `[diag]` 实测 2026-11-11 15:18:06 |
| 每请求工具 schema | 36,098 字符 | 同上 |
| 每请求历史上下文 | 34,573 字符 / 122 条消息 | 同上 |
| 单次请求 token | 43,870 | `conversations.token_usage` |

**成本口径**：工具 schema 是**每轮固定发送**，成本按「字符数 × 1」计。调用次数只用于判断工具是否值得保留，**不与字符数相乘**——相乘会把固定成本和使⽤价值混在一起，得出相反结论（例如 `uapi_misc_hotboard` 1,257 字符 × 106 次看似最贵，实际它 schema 小、用得多，是最该留的）。

---

## 三、动作清单

### A 档：删 enum，工具保留

| ID | 工具 | 参数 | 现 schema | 删后 | 省 | 风险 |
|---|---|---|---|---|---|---|
| A1 | `uapi_misc_worldtime` | `city` | 8,299 | ~220 | **8,080** | 无 |

**为什么是「删」而不是「削」**：该 enum 是完整 IANA 时区表（400+ 项），占 schema 97.4%。参数的 `description` 已经写明「填标准 IANA 时区名，如 `Asia/Shanghai`」，模型掌握格式即可正确填写，不需要选项表。整段删掉，比截短更能消除同类 enum 再次被倒灌进来的土壤。

验证：应用后调用一次世界时间查询 `Asia/Tokyo`，确认返回正确。

### B 档：移除工具（零调用或完全重复）

| ID | 工具 | schema | 观察调用 | 理由 |
|---|---|---|---|---|
| B1 | `uapi_translate_text` | 841 | 0 | 与 `uapi_ai_translate` **参数结构完全相同**（均仅一个目标语言参数 + 同一份 109 项语言枚举），只是后端不同（机器翻译 vs AI 翻译），保留 AI 版 |
| B2 | `uapi_misc_holiday-calendar` | 1,135 | 0 | 零调用，节假日查询不在角色日常需求内 |
| B3 | `uapi_image_bing-daily` | 653 | 0 | 零调用，每日一图属装饰性能力 |

验证：移除前 grep 确认无插件按名引用这几个工具。

**A + B 合计：10,709 字符 ≈ 6,693 token ≈ 15.3% 账单。**

### C 档：待定

| ID | 工具 | schema | 观察调用 | 情况 |
|---|---|---|---|---|
| C1 | `web_search_tavily` | 1,409 | 0 | 备用搜索后端（主用 `web_search_bocha`），砍后少一层容灾，值 2.0% 账单 |

**判定要看两个事实**：

1. `web_search_bocha` 过去一个月的失败率——若从未失败，tavily 的容灾价值是纯假设，砍掉无损失；
2. bocha 挂掉时，系统是「报错给用户」还是「静默不回应」——若是后者，没有容灾的代价是「用户发问没回应」，这比多花 2% 严重。

观察期结束后查 bocha 的失败日志再定。

### 长尾观察（两周后再定）

`clone_tts`、`qq_set_nickname`、`behavior_tune`、`task_ignore`、`uapi_misc_weather`、`list_installed_plugins`、`search_plugins`、`memory_recall`、`get_weather_meme`

观察调用 1–4 次。其中 `task_ignore` / `behavior_tune` 属管理类、可能偶发需要；`list_installed_plugins` / `search_plugins` 为只读且字符很小。两周后仍为 0–2 次且无管理需求，则整工具移除。

### 明确不动

| 工具 | enum 字符 | 调用 | 理由 |
|---|---|---|---|
| `uapi_misc_hotboard` | 528 | 106 | 选项边界类 enum（平台列表），删了模型会编造平台名 |
| `uapi_saying_random` | 592 | 2 | 选项边界类 enum（风格枚举），工具依赖该选项 |

> `uapi_saying_random`：enum 保留，但工具本身进入两周观察。若调用仍为 0–2 次，可整工具移除——「enum 该不该留」和「工具该不该留」是两个独立问题。

---

## 四、判断标准：enum 的两分法（本清单最有价值的方法论）

以后每加一个带 `enum` 的工具，都用这一条判断：

> **如果模型不知道选项就会填错**（填错后工具报错或行为异常，例如编造不存在的平台名），该 enum 属于**「选项边界类」→ 必须保留**。
>
> **如果模型掌握格式就能正确填写**（例如 IANA 时区名、ISO 语言代码、日期格式），该 enum 属于**「格式说明类」→ 应删除，改用 `description` 说明格式并给一个示例**。

典型案例：

| 类型 | 案例 | 处理 |
|---|---|---|
| 选项边界类 | `misc.hotboard` 的平台列表（抖音/微博/B站/知乎…） | 保留——模型必须在封闭集合内选择 |
| 格式说明类 | `misc.worldtime` 的 IANA 时区列表 | 删除——模型知道格式，给 `Asia/Shanghai` 一个示例即可 |
| 格式说明类 | `translate.*` 的 109 项语言代码 | 工具保留时可删 enum 只留示例；本文档中该工具整体移除 |

---

## 五、更正声明（写进文件，供后续复用）

**`uninstall_plugin` 不是当前存在的风险。**

- 原始假设：这是内置插件管理工具，属安全风险，应排除。
- 事实：**已于 2026-10-31 15:17:49 移除**。日志原文：`移除了插件 astrbot_plugin_dafeiyu 的处理函数 uninstall_plugin (191)`。
- 此前统计到的 2 次调用来自 10-31 的历史日志，只说明当时存在，不代表当前系统具备该能力。

**`search_plugins` / `list_installed_plugins` 也不构成安全风险。**

两者是 `astrbot_plugin_shihai_behavior/dafeiyu_core.py:1592 / :1609` 的 `@filter.llm_tool`，**只读**——一个搜插件市场、一个列已装插件，没有任何安装或卸载能力。安装只走 `/装插件` 命令，且有 `_check_owner` 校验。

---

## 六、实施注意事项

### 1. 统计脚本要只统计「当前已注册工具」

扫日志统计调用频率时，必须用当前请求的 `tool_n` 工具清单做过滤。历史日志里可能残留已移除工具（如 `uninstall_plugin`）的调用记录，直接计数会得出「当前系统存在该能力」的错误结论。

### 2. 应用脚本的设计要求（`tool_trim_apply.py`）

- 读 `tool_trim_plan.json`，**只处理 `actions`**；`pending` / `low_priority` / `exclusions` 仅作记录，不执行；
- `delete_enum`：把参数里的 `enum` 字段整个删掉，保留 `description`；
- `remove_tool`：从 `api_registry.py` 的 `API_DEFINITIONS` 中移除对应条目；
- 应用前自动备份 registry 文件（带时间戳，不覆盖）；
- 应用后校验每个工具的新 schema 长度是否符合 `expected_schema_chars`（容差 ±15%），不符则自动回滚；
- 备份路径写进输出，便于人工核对。

### 3. 应用时机与变量隔离

**等这一周 `listen_hint` 观察结束再应用。** 砍工具虽对记忆与人格零影响，但它是本轮唯一会改变请求构成的动作，须单独成一个变量、单独验证一周，不与其他改动叠加。

**应用顺序**：A1（单项最大、零风险）→ 验证 → B1/B2/B3 → 验证。每步单独跑一次请求、取 `[diag]` 对比 `tool_chars` 变化。

### 4. 生效方式

`api_registry.py` 的改动**需要重启 AstrBot 才生效**（无热重载）。重启后用 `[diag]` 的 `tool_chars` 与 `tool_top` 复核实际值。

---

## 七、状态

- 2026-11-11：清单定稿，**不执行**，等 `listen_hint` 观察期结束。
- 执行前需确认：`web_search_bocha` 失败率（决定 C1）、长尾工具两周调用次数（决定 low_priority）。
