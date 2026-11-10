# 识海·一致性守护（astrbot_plugin_shihai_guard）

给识海家族加的一层**只读守护**：它不改情绪场、不改图谱、不改念头，只在
“把什么塞进提示词”和“生成后要不要降级”这两个关口上做确定性检查。

对应论文《识海》的 §7.5（时序锚定）、§7.6（quote 通道隔离）、§18（表达归界）。

## 四条不变量

| 不变量 | 内容 | 落点 |
|---|---|---|
| I1 时序单调 | 进上下文的记忆都带 `when`；同实体新旧事实并存时新者在前 | `guard/timeline.py` |
| I2 通道隔离 | `type=quote` 只走 `bot_quotes`（few-shot），不进 G_active / 锚点候选 / R_t | `guard/channels.py` |
| I3 表达归界 | 性格层不写状态断言，风格由 σ(ψ) 渲染；意图包不放身份与文体 | `guard/persona.py` |
| I4 解释权唯一 | 状态字段与风格字段键空间不相交，冲突以状态为准 | `guard/persona.arbitrate` |

## 数据契约

只读共享目录（`data/plugin_data/shihai/`）：

- `shihai_graph.json` —— 节点 `{type, content, a, E, created, last_active}`
- `mind_state.json` —— 情绪场 `e`
- `psyche_state.json` —— 人格 `psi`

写入：`guard_trace.jsonl`（每次生成一条审计记录，含 I1–I4 结果与累计违规计数）。

## 配置

`data/config/astrbot_plugin_shihai_guard_config.json`

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关；关掉即“无守护”对照组，用于 A/B |
| `r_t_limit` | 4 | R_t 条数 |
| `r_t_hours` | 24 | R_t 时间窗 |
| `inject_style` | true | 是否注入 ψ 渲染的表达参数 |
| `postcheck` | true | 生成后强度/时态校验 |
| `intensity_tol` | 0.35 | 强度偏差容忍，超过即降级措辞 |

## 测试

```bash
cd data/plugins/astrbot_plugin_shihai_guard
python3 -m pytest tests -q      # 8 passed
```

单测覆盖：六档时间标签边界、已完成事项的排序、quote 三处隔离、静态断言扫描、
σ(ψ) 的单调与有界、字段重叠拒绝、强度降级、催促线索检出。

## 上线顺序建议

1. 先开 I2（`channels`）：收益确定、零风险，只是把 quote 从事实通道拉出来；
2. 再开 I1（`timeline`）：新增 `when` 字段，需要图谱里 `created` 正确；
3. 最后开 I3（`persona`）：要重写性格层，工作量最大，建议先用 `inject_style=false` 观察一周。

## 边界

不宣称消除故障，只把故障转成可计数：违规率写入 `guard_trace.jsonl`，
可按版本对比。表达强度校验用词表而非语义模型，对反讽/引用会误判，
因此只做降级，不做拦截发送。
