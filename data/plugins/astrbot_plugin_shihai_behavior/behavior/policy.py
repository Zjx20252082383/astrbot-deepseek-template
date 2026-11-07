"""策略选择引擎（论文 §5.9 策略选择方程）。

    π(e, a, ψ, u, g) = argmax_x Q(e, a, x) + β·Σ_i g_i(t)·φ_i(x)

Q 来自 L_adapt（用户反馈驱动，核心 z(t) 的 q_table）；
φ_i 是行为对第 i 维目标的贡献度（behaviors.json 里的 phi）；
β 是目标-策略耦合系数（本插件配置）。
物理含义：即使主人没说话，g_relation 也会驱动主动关心；
g_avoid 在冷战后驱动主动和解、不过度打扰。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Tuple

from .actions import check_requires


class PolicyEngine:
    def __init__(self, beta: float = 0.8):
        self.beta = max(0.0, min(3.0, float(beta)))

    def score(self, state: Dict[str, Any], action: Dict[str, Any]) -> float:
        """单行为打分：Q + β·(g·φ)。state 为 z(t) 快照（mind_state.json）。"""
        q = float((state.get("q_table", {}) or {}).get(action.get("id", ""), 0.0))
        g = state.get("goals", {}) or {}
        goal_term = sum(float(g.get(gid, 0.0)) * float(w)
                        for gid, w in (action.get("phi") or {}).items())
        return q + self.beta * goal_term

    def select(
        self, state: Dict[str, Any], behaviors: List[Dict[str, Any]],
        anchor_kinds: List[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], float]:
        """从涌现候选中选一个行为。

        候选条件：enabled + proactive + 锚点匹配 + requires 满足。
        返回 (action, 匹配的锚点, 得分)；无候选返回 (None, None, 0)。
        """
        best: Optional[Tuple[float, Dict[str, Any], Dict[str, Any]]] = None
        for act in behaviors:
            if not act.get("enabled", True) or not act.get("proactive"):
                continue
            if act.get("needs"):
                continue  # 需要外部适配器的行为，v1 看门狗不自动触发
            anchors = act.get("anchor") or ["recent"]
            matched = next((a for a in anchor_kinds if a in anchors), None)
            if matched is None:
                continue
            if not check_requires(act, state):
                continue
            s = self.score(state, act) + random.random() * 0.05
            if best is None or s > best[0]:
                anchor = next((a for a in (state.get("fire_pending", {})
                                           or {}).get("anchors", [])
                               if a.get("kind") == matched), {})
                best = (s, act, anchor or {"kind": matched, "node": "", "content": ""})
        if best is None:
            return None, None, 0.0
        return best[1], best[2], best[0]

    def rankings(self, state: Dict[str, Any],
                 behaviors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """全部行为打分视图（behavior_list 工具用）。"""
        rows = []
        for act in behaviors:
            rows.append({
                "id": act.get("id", ""),
                "label": act.get("label", ""),
                "category": act.get("category", ""),
                "proactive": bool(act.get("proactive")),
                "enabled": bool(act.get("enabled", True)),
                "needs": act.get("needs", ""),
                "requires": act.get("requires", {}),
                "emergent": (not act.get("needs")) and check_requires(act, state),
                "score": round(self.score(state, act), 3),
                "q": round(float((state.get("q_table", {}) or {}).get(
                    act.get("id", ""), 0.0)), 3),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows
