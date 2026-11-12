# 逐字对话归档与原文锚点 — 方案 A 设计（待鑫鑫确认）

## 目标
1. 每轮 user / assistant 逐字**持续归档**，不再出现 11-10 后的断档。
2. 长期可检索逐字原文，**按需调用、不常驻前缀** → 不破坏已调好的 98% 缓存。
3. 历史 1173 条从 livingmemory 迁移，不丢旧数据。
4. 与现有 memory_v2 融合，不改动摘要/心智 ODE 链路。

---

## 1. 新增存储 `transcript.db`（SQLite + FTS5）

- 位置：`data/plugin_data/astrbot_plugin_qq_toolbox/memory_v2/transcript.db`
- `*.db` 已被 `.gitignore` 排除，**不入公开仓**；启用 WAL，单文件 append-only。

```sql
CREATE TABLE IF NOT EXISTS transcript(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,          -- unix 秒
  origin      TEXT,                  -- unified_msg_origin（会话标识）
  role        TEXT NOT NULL,         -- user / assistant
  sender_id   TEXT,
  sender_name TEXT,
  group_id    TEXT,
  content     TEXT NOT NULL,         -- 逐字原文
  kind        TEXT DEFAULT 'text',   -- text / voice / interrupted
  msg_id      TEXT,                  -- 平台消息 id，去重
  turn_id     TEXT                   -- 同一轮 user→assistant 关联
);
CREATE INDEX idx_origin_ts ON transcript(origin, ts);
CREATE UNIQUE INDEX uq_msg ON transcript(msg_id) WHERE msg_id IS NOT NULL;

-- 全文检索（FTS5 外部内容表，与 transcript 同步）
CREATE VIRTUAL TABLE transcript_fts USING fts5(
  content, origin, content='transcript', content_rowid='id', tokenize='unicode61');
```

要点：单文件、写入毫秒级、不随条数增长重写整库（对比 JSON 全量重写）；只存文本，不存图片/音频二进制。

---

## 2. 新增模块 `modules/transcript_store.py`

类 `TranscriptStore`：
- `append(...)`：插入，`msg_id` 去重（INSERT OR IGNORE）。
- `search(keyword, origin=None, since=None, role=None, limit=...)`：FTS5 + 应用层字符二元组混合检索（unicode61 对中文按字切，grams 兜底）。
- `get_turn(turn_id)`、`get_range(t0, t1)`、`count()`、`latest_ts()`。
- 本地操作毫秒级，用 `asyncio.to_thread` 包裹，不阻塞事件循环。

---

## 3. 挂载点改动（最小侵入，复用现有钩子）

| 方向 | 挂载点 | 改动 |
|---|---|---|
| 用户消息 | `memory_observe`（已有过滤：非 bot、非命令） | 生成 `turn_id`，把 user 原文 append 进 transcript |
| 小满回复 | `remember_sent_line`（`after_message_sent`，能拿到**实际发出**的文本，含语音/被打断） | 除现有 `_append_history` 外，同时 append（role=assistant，复用同一 turn_id） |
| 初始化 | `setup_memory` | 初始化 `TranscriptStore`（路径同 base） |

现有 `_append_history` 补的是**窗口内**上下文，截断即丢；新增归档是**窗口外**长期层，两者并行不冲突。

---

## 4. 原文锚点：`EventItem` 加字段

`memory_v2.py` 的 `EventItem` 新增：
```python
transcript_turn_id: str = ""   # 默认空，向后兼容旧 JSON
```
固结 `commit` 时填入对应 `turn_id`；需要原文时 `TranscriptStore.get_turn()` 回查，**不复制全文进 JSON**，保持 `event_snapshot.json` 精简。替代原本指向停用 livingmemory 的 `doc_id` 回查。

---

## 5. 新增工具 `recall_transcript`（@filter.llm_tool）

- 参数：`keyword`（必填）、`days`（默认 30）、`role`（可选）。
- 返回：匹配逐字片段，最多 5 条、每条 ≤200 字、带相对时间，超预算截断。
- 仅在她需要确认原话/回忆某次具体对话时主动调用；**不常驻前缀**。
- 对缓存零影响：每轮固定成本不增加；工具 schema 进 tools 定义（本就是固定前缀、会被缓存），新增一个工具的前缀增量极小。

---

## 6. 历史迁移 `tools/migrate_livingmemory_transcript.py`

从 `conversations.db` 的 messages 表读 1173 条 → 字段映射 → 去重写入 `transcript.db`。
- 幂等可重复跑（内容哈希/msg_id 去重）；先备份、支持 `--dry-run`；跑完校验 count、时间范围、抽样检索。

---

## 7. 成本与缓存影响

- 写入：本地 SQLite，**零 API、零 token**。
- 读取：仅工具调用时偶发，片段计入当轮输入；平均每轮成本基本不变。
- 缓存：归档/锚点不进固定前缀，98% 命中率不受影响。
- 体积：纯文本，1000 条约 0.3–0.6 MB，月增 <1 MB。

---

## 8. 分阶段实施（每步可独立验证、可回滚）

1. 写 `transcript_store.py` + 建库，单测插入/检索/去重。
2. 迁移脚本导入历史 1173 条并验证。
3. 挂载 `memory_observe` / `remember_sent_line`，重启观察双向落库。
4. `EventItem` 加字段 + 固结回填。
5. 注册 `recall_transcript`，端到端测试。

每步先备份、`py_compile` 校验，不影响运行中的机器人，直到重启。

---

## 边界与如实说明

- **语音**：归档 TTS 实际朗读文本（`_shihai_voice_text` 已能拿到），不存音频。
- **图片**：归档路径/用户说明，不存图像内容（与现状"只看到路径"一致）。
- **缺口**：11-10 22:15 至本功能上线前的逐字内容，仍在窗口内的可从 `data_v4` 的 conversations.content 补；已出窗口的无法逐字恢复，只能靠摘要。
