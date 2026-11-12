"""意图包组装与格式化（输出协议：动力学内核 → LLM）。§12.1/§12.5 瘦身版。

    I_t = { G_active,  E_t,  M*_t,  intent-type }

- G_active   : 高激活记忆节点列表（主动开口时锚点排第一；被动 3 条 / 主动 4 条，
              每条带大概时间——时间纪律与一致性守护 I1 依赖这个标签）。
              节点 ID 与激活值不再打印：来源可指回由代码校验（锚点闸）保证，
              打印出来既费 Token 又诱导模型"报数值"。
- E_t        : 状态断言一行——主导情绪+强度，另带 ≥0.25 的次情绪（≤2 个）。
              不再给六维全向量：§12.1 意图包不含完整情绪向量，
              LLM 看得越少，越不容易把"此刻"编成"一直"。
- M*_t       : 主导动机（主动=行为标签；被动=g 向量最高维）
- P_t        : 念头强度只留在 dict 里供 Web/审计，不再拼进提示词——
              开口决策已在内核完成，P_t 对语言生成没有信息量（§12.5）。

省 Token 口径（§12.5 提示词瘦身）：被动意图包 ≤200 字；指令只留
"情绪一句、记忆引用一句、边界一句"，由 main.py 的 intent_instruction_* 提供。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_E_CN = {"happy": "开心", "sad": "难过", "angry": "生气",
         "fear": "害怕", "love": "依恋", "surprise": "惊讶"}

_MOTIVE_CN = {
    "g_relation": "维持与用户的关系",
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
            g_active.insert(0, {"id": node, "content": content[:120],
                                "when": str((anchor or {}).get("when", "") or ""),
                                "a": 1.0})
    cap = 3 if intent_type == "被动应答" else 4
    e = state.get("e", {}) or {}
    e_values = [float(v) for v in e.values()] or [0.0]
    e_dom = max(e, key=lambda k: float(e.get(k, 0.0))) if e else "calm"
    secondary = [(d, round(float(e.get(d, 0.0)), 2)) for d in sorted(
        (k for k in e if k != e_dom and float(e.get(k, 0.0)) >= 0.25),
        key=lambda k: float(e.get(k, 0.0)), reverse=True)][:2]
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
        "G_active": g_active[:cap],
        "recent_talks": list(state.get("recent_talks", []) or [])[:3],
        "E_t": {
            "dominant": e_dom,
            "intensity": round(max(e_values), 2),
            "secondary": secondary,
        },
        "M_t": {"motive": motive, "source": motive_src,
                "g": state.get("goals", {})},
        "P_t": {
            "urge": round(float(state.get("urge", 0.0)), 2),
            "threshold": round(float(theta.get("fire_threshold", 2.1))
                               + float(theta.get("streak_gate", 0.0)), 2),
        },
        "intent_type": intent_type,
    }


def _who(row: Dict[str, Any]) -> str:
    """给记忆标主语：用户说的话不是"我"的经历，避免生成时把用户的事写成自己的。"""
    sender = str(row.get("sender", "owner") or "owner")
    return "我说的：" if sender == "bot" else "用户说："


def format(pkg: Dict[str, Any]) -> str:
    """意图包 → 给 LLM 看的固定结构文本（§12.5：五行以内）。"""
    g_rows = "；".join(
        f"（{m.get('when', '')}）{_who(m)}{m.get('content', '')}"
        for m in pkg["G_active"]) or "无"
    talks = pkg.get("recent_talks") or []
    talk_rows = "；".join(
        f"{t.get('when', '')}{_who(t)}「{t.get('content', '')}」" for t in talks) or "（近期无记录）"
    e = pkg["E_t"]
    e_parts = [f"{_E_CN.get(e['dominant'], e['dominant'])}"
               f"{int(float(e['intensity']) * 100)}%"]
    e_parts += [f"{_E_CN.get(d, d)}{int(float(v) * 100)}%"
                for d, v in e.get("secondary", [])]
    m = pkg["M_t"]
    return (
        "【意图包】\n"
        f"最近聊过：{talk_rows}\n"
        f"记得的事：{g_rows}\n"
        f"此刻情绪：{'、'.join(e_parts)}\n"
        f"动机：{m['motive']}｜类型：{pkg['intent_type']}"
    )
