"""意图包组装与格式化（输出协议：动力学内核 → LLM）。

    I_t = { G_active,  E_t,  M*_t,  P_t,  intent-type }

- G_active   : 当前高激活记忆节点列表（附记忆 ID，来自核心快照 g_active）
- E_t        : 目标情绪（情绪场 + 主导分量）
- M*_t       : 主导动机（主动开口时=行为标签；应答时=g 向量最高维）
- P_t        : 念头强度 u(t)（对照有效开口阈值）
- intent-type: 被动应答 / 主动关心 / 主动回忆

LLM 的输入只有这个结构化包 + 简短固定指令（指令文案在本插件配置）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_E_CN = {"happy": "开心", "sad": "难过", "angry": "生气",
         "fear": "害怕", "love": "依恋", "surprise": "惊讶"}

_MOTIVE_CN = {
    "g_relation": "维持与主人的关系",
    "g_memory": "整理巩固记忆",
    "g_self": "自我表达与成长",
    "g_avoid": "回避冲突、避免被讨厌",
}


def assemble(state: Dict[str, Any], intent_type: str = "被动应答",
             action: Optional[Dict[str, Any]] = None,
             anchor: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 z(t) 快照组装意图包。state 为 mind_state.json 的内容。"""
    g_active: list = list(state.get("g_active", []) or [])
    # 主动开口时，这次的事由（锚点）排第一位（带时间标签）
    node = str((anchor or {}).get("node", "") or "")
    if node:
        g_active = [m for m in g_active if m.get("id") != node]
        content = str((anchor or {}).get("content", "") or "")
        if content:
            g_active.insert(0, {"id": node, "content": content[:160],
                                "when": str((anchor or {}).get("when", "") or ""),
                                "a": 1.0})
    e = state.get("e", {}) or {}
    e_values = [float(v) for v in e.values()] or [0.0]
    e_dom = max(e, key=lambda k: float(e.get(k, 0.0)))
    if action:
        motive = str(action.get("label", "主动开口"))
        motive_src = str(action.get("id", ""))
    else:
        goals = state.get("goals", {}) or {}
        g_top = max(goals, key=lambda k: float(goals.get(k, 0.0))) if goals else "g_relation"
        motive = _MOTIVE_CN.get(g_top, g_top)
        motive_src = g_top
    theta = state.get("theta", {}) or {}
    return {
        "G_active": g_active[:5],
        "recent_talks": list(state.get("recent_talks", []) or [])[:4],
        "E_t": {
            "vector": {d: round(float(v), 2) for d, v in e.items()},
            "dominant": e_dom,
            "intensity": round(max(e_values), 2),
        },
        "M_t": {"motive": motive, "source": motive_src, "g": state.get("goals", {})},
        "P_t": {
            "urge": round(float(state.get("urge", 0.0)), 2),
            "threshold": round(float(theta.get("fire_threshold", 2.1))
                               + float(state.get("streak_gate", 0.0)), 2),
        },
        "intent_type": intent_type,
    }


def _who(row: Dict[str, Any]) -> str:
    """给记忆标主语：主人说的话不是"我"的经历，避免生成时把主人的事写成自己的。"""
    sender = str(row.get("sender", "owner") or "owner")
    return "我说的：" if sender == "bot" else "主人说："


def format(pkg: Dict[str, Any]) -> str:
    """意图包 → 给 LLM 看的固定结构文本。"""
    g_rows = "；".join(
        f"[{m.get('id', '')}] {('（' + m['when'] + '）' if m.get('when') else '')}"
        f"{_who(m)}{m.get('content', '')}({m.get('a', 0)})"
        for m in pkg["G_active"]) or "无"
    talks = pkg.get("recent_talks") or []
    talk_rows = "；".join(
        f"{t.get('when', '')}{_who(t)}「{t.get('content', '')}」" for t in talks) or "（近期无记录）"
    e = pkg["E_t"]
    e_str = "、".join(f"{_E_CN.get(d, d)}{int(float(v) * 100)}%"
                      for d, v in e["vector"].items() if float(v) >= 0.05) or "平静"
    m = pkg["M_t"]
    p = pkg["P_t"]
    return (
        "【意图包 I_t】\n"
        f"最近聊过（时序锚，注意事情的新旧）：{talk_rows}\n"
        f"G_active（高激活记忆）：{g_rows}\n"
        f"E_t（目标情绪）：{e_str}｜主导：{_E_CN.get(e['dominant'], e['dominant'])}"
        f" {int(float(e['intensity']) * 100)}%\n"
        f"M*_t（主导动机）：{m['motive']}\n"
        f"P_t（念头强度）：{p['urge']}（开口阈值 {p['threshold']}）\n"
        f"intent-type：{pkg['intent_type']}"
    )
