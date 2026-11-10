"""时序锚定（论文 §7.5）：相对时间标签 when 与意图包字段 R_t。

不变量 I1（时序单调性）：
    任何注入 LLM 的记忆节点，必须带一个由写入时刻算出、每次生成重算的 when 标签；
    同一实体的新旧事实若同时被召回，新事实必须排在旧事实之前。

本模块是纯函数层：只读 (id, content, created, type, a)，不写回，不触碰快状态。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Sequence

# R_t 只取这三类：事件、情绪时刻、高光/创伤。fact 是"概念"，不是"发生过的事"。
R_T_TYPES = ("event", "emotion", "highlight")


def when_label(dt_seconds: float, now: float | None = None) -> str:
    """把写入时刻到当前时刻的间隔映射为自然语言标签（论文表 7-3）。

    dt_seconds < 0 视为 0（时钟回拨/跨机时区误差保护）。
    """
    dt = max(0.0, float(dt_seconds))
    h = dt / 3600.0
    d = dt / 86400.0
    if h < 1:
        return "刚刚聊过"
    if h < 24:
        return f"{int(h)} 小时前聊过"
    if d < 2:
        return "昨天聊过"
    if d < 7:
        return f"{int(d)} 天前聊过"
    if d < 30:
        return f"{int(d / 7)} 周前聊过"
    return f"{max(1, int(d / 30))} 个月前聊过"


def build_r_t(
    nodes: Iterable[Dict[str, Any]],
    now: float | None = None,
    hours: float = 24.0,
    limit: int = 4,
    skip_types: Sequence[str] = ("quote",),
) -> List[Dict[str, Any]]:
    """构造时序锚 R_t：最近 hours 小时内三类节点，按写入时刻倒序取前 limit 条。

    返回 [{id, content, when, ts, type, a}]，where 标签已按 now 重算。
    排序键唯一（created, id），保证同刻写入时结果稳定，不随迭代顺序漂移。
    """
    now = time.time() if now is None else float(now)
    horizon = now - hours * 3600.0
    rows: List[Dict[str, Any]] = []
    for nd in nodes:
        try:
            ntype = str(nd.get("type", "fact"))
            if ntype not in R_T_TYPES or ntype in skip_types:
                continue
            created = float(nd.get("created", 0.0))
            if created < horizon or created > now + 60.0:
                continue
            content = str(nd.get("content", "")).strip()
            if not content:
                continue
            rows.append({
                "id": str(nd.get("id", "")),
                "content": content[:120],
                "when": when_label(now - created, now),
                "ts": created,
                "type": ntype,
                "a": float(nd.get("a", 0.0)),
            })
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: (-r["ts"], r["id"]))
    return rows[:limit]


def render_r_t(rows: Sequence[Dict[str, Any]]) -> str:
    """把 R_t 渲染成意图包首行（模型读得懂的自然语言）。"""
    if not rows:
        return "R_t：最近 24 小时没有新记录（不要假设用户在推进任何旧事务）"
    parts = [f"「{r['when']}」{r['content']}" for r in rows]
    return "R_t（按时间倒序，第一条即最新事实）：" + "；".join(parts)


def annotate_g_active(
    rows: Sequence[Dict[str, Any]],
    now: float | None = None,
) -> List[Dict[str, Any]]:
    """给 G_active 的每条记忆补 when 标签（不排序、不改内容，只加字段）。"""
    now = time.time() if now is None else float(now)
    out: List[Dict[str, Any]] = []
    for nd in rows:
        row = dict(nd)
        try:
            created = float(row.get("created", now))
            row["when"] = when_label(now - created, now)
        except (TypeError, ValueError):
            row["when"] = ""
        out.append(row)
    return out


def check_temporal_order(rows: Sequence[Dict[str, Any]]) -> bool:
    """不变量 I1 的自检：R_t 必须按写入时刻严格不升。"""
    ts = [float(r.get("ts", 0.0)) for r in rows]
    return all(ts[i] >= ts[i + 1] for i in range(len(ts) - 1))


TEMPORAL_DISCIPLINE = (
    "【时序纪律】每条记忆与 R_t 都带 when 标签；几小时/几天前的事已经是过去，"
    "引用时用过去式或“上次”；若某事明显已经完成（考过的试、赴过的约），"
    "禁止追问“好了没”或催促；以 R_t 最新一条判断事情的当前状态。"
)
