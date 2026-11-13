"""g(t) 目标向量漂移（论文 §10）。

g(t) 是 z(t) 的内生目标分量，属于核心动力学，留在 Core-Mind：
    g_relation 互动频繁时↑，久无互动缓降
    g_memory  深夜↑（梦境固化窗口）
    g_self    长时间没有自我表达时↑
    g_avoid   负反馈后↑，随后缓慢回落
行为插件（shihai_behavior）读取 g 参与 π 打分，但不写 g。
"""

from __future__ import annotations

from .state import MindState, clamp


REL_CONVERGE_RATE = 0.08  # g_relation 向实测关系强度收敛的速率（1/h）


def goal_drift(
    state: MindState, dt_hours: float, hour: int,
    interaction_recent: bool, negative_recent: bool,
    last_self_expression_age_h: float = 24.0,
    relation_strength: float | None = None,
) -> None:
    if dt_hours <= 0:
        return
    g = state.goals
    # g_relation：给了实测关系强度（§10.6 RelationshipMap）就向它收敛，
    # 否则退回原布尔推拉（只有"刚聊过/久无互动"，不区分和谁、多深）
    if relation_strength is not None:
        g["g_relation"] = clamp(
            g["g_relation"]
            + REL_CONVERGE_RATE * (relation_strength - g["g_relation"]) * dt_hours,
            0.0, 1.0)
    elif interaction_recent:
        g["g_relation"] = clamp(g["g_relation"] + 0.04 * dt_hours, 0.0, 1.0)
    else:
        g["g_relation"] = clamp(g["g_relation"] - 0.015 * dt_hours, 0.05, 1.0)
    # g_memory：深夜↑（梦境固化窗口 0-6 点）
    if 0 <= hour < 6:
        g["g_memory"] = clamp(g["g_memory"] + 0.06 * dt_hours, 0.0, 1.0)
    else:
        g["g_memory"] = clamp(g["g_memory"] - 0.008 * dt_hours, 0.05, 1.0)
    # g_self：超过一天没有自我表达时积攒表达欲
    g["g_self"] = clamp(
        g["g_self"] + 0.006 * dt_hours * min(4.0, last_self_expression_age_h / 12.0),
        0.0, 1.0)
    # g_avoid：负反馈后↑，随后缓慢回落
    if negative_recent:
        g["g_avoid"] = clamp(g["g_avoid"] + 0.10, 0.0, 1.0)
    else:
        g["g_avoid"] = clamp(g["g_avoid"] - 0.01 * dt_hours, 0.05, 1.0)
