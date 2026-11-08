"""策略选择引擎（论文 §5.9 策略选择方程 + 整合稿 §17.4 内生探索）。

    b* = argmax_b [ (1 − ε)·U(b) + ε·n(b) ]                        (17-3)
    ε(g_relation) = ε_max / [1 + exp(k·(g_relation − g₀))]         (17-4)

U(b) = Q + β·Σ g_i·φ_i（§5.9 策略打分，来自 L_adapt 的 q_table 与 behaviors.json 的 phi）；
n(b) 为动作新颖性权重（§17.4 表 17-3：绘画0.80/哼唱0.60/检索0.50/记忆整理0.40/
反思日记0.30/对话回复0.10，behaviors.json 每行为配 novelty）；
ε 为探索系数：g_relation 高（互动频繁）→ ε→0 优先维持关系；
g_relation 低（长期沉默）→ ε 升至 0.30 以上 → 自发探索，避免完全沉默。
默认 ε_max=0.35、g₀=0.5、k=6（工程先验，§17.6 未验证项：最优标定留待用户对照实验）。
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Tuple

from .actions import check_requires


class PolicyEngine:
    def __init__(self, beta: float = 0.8,
                 explore_enabled: bool = True,
                 epsilon_max: float = 0.35,
                 epsilon_g0: float = 0.5,
                 epsilon_k: float = 6.0):
        self.beta = max(0.0, min(3.0, float(beta)))
        self.explore_enabled = bool(explore_enabled)
        self.epsilon_max = max(0.0, min(1.0, float(epsilon_max)))
        self.epsilon_g0 = max(0.0, min(1.0, float(epsilon_g0)))
        self.epsilon_k = max(0.1, float(epsilon_k))

    def explore_epsilon(self, g_relation: float) -> float:
        """式 17-4：探索系数由关系维护目标驱动。"""
        if not self.explore_enabled:
            return 0.0
        return self.epsilon_max / (
            1.0 + math.exp(self.epsilon_k * (float(g_relation) - self.epsilon_g0)))

    def score(self, state: Dict[str, Any], action: Dict[str, Any],
              epsilon: float = 0.0) -> float:
        """单行为打分：U(b) = Q + β·(g·φ)；加探索项 (1−ε)·U + ε·n（式 17-3）。"""
        q = float((state.get("q_table", {}) or {}).get(action.get("id", ""), 0.0))
        g = state.get("goals", {}) or {}
        goal_term = sum(float(g.get(gid, 0.0)) * float(w)
                        for gid, w in (action.get("phi") or {}).items())
        u = q + self.beta * goal_term
        novelty = float(action.get("novelty", 0.1))
        return (1.0 - epsilon) * u + epsilon * novelty

    def select(
        self, state: Dict[str, Any], behaviors: List[Dict[str, Any]],
        anchor_kinds: List[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], float]:
        """从涌现候选中选一个行为。

        候选条件：enabled + proactive + 锚点匹配 + requires 满足。
        返回 (action, 匹配的锚点, 得分)；无候选返回 (None, None, 0)。
        """
        best: Optional[Tuple[float, Dict[str, Any], Dict[str, Any]]] = None
        eps = self.explore_epsilon(float(
            (state.get("goals", {}) or {}).get("g_relation", 0.5)))
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
            s = self.score(state, act, eps) + random.random() * 0.05
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
        eps = self.explore_epsilon(float(
            (state.get("goals", {}) or {}).get("g_relation", 0.5)))
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
                "score": round(self.score(state, act, eps), 3),
                "q": round(float((state.get("q_table", {}) or {}).get(
                    act.get("id", ""), 0.0)), 3),
            })
        rows.sort(key=lambda r: r["score"], reverse=True)
        return rows
