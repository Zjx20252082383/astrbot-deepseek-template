"""通道隔离（论文 §7.6）：quote 节点只作风格语料，绝不进入事实召回。

不变量 I2（通道隔离）：type=quote 的节点不出现在
    (1) G_active（事实召回）
    (2) 锚点候选（anchor_candidates）
    (3) R_t（时序锚）
三处输出；只经 bot_quotes 独立通道（few-shot）进入提示词。

理由：若 quote 参与常规召回，"自己过去说过的话"会被当作记忆事实复述，
越常说某类话越容易被再次召回，最终话题收敛到少数句式上——这是上下文
割裂与自我指涉循环的一个来源。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

QUOTE_TYPE = "quote"


def is_quote(node: Dict[str, Any]) -> bool:
    return str(node.get("type", "")).lower() == QUOTE_TYPE


def filter_facts(
    rows: Sequence[Dict[str, Any]],
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """G_active 过滤：剔除 quote 类型。"""
    out = [r for r in rows if not is_quote(r)]
    return out[:limit] if limit is not None else out


def filter_anchors(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """锚点候选过滤：剔除 quote 类型（quote 没有"事由"，只有语气）。"""
    return [c for c in candidates if not is_quote(c)]


def filter_quotes(
    rows: Sequence[Dict[str, Any]],
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """bot_quotes 通道：只取 quote 节点，按激活值降序（风格丰富度优先）。"""
    qs = [r for r in rows if is_quote(r)]
    qs.sort(key=lambda r: (-float(r.get("a", 0.0)), str(r.get("id", ""))))
    return qs[:limit]


def render_bot_quotes(rows: Sequence[Dict[str, Any]]) -> str:
    """渲染 few-shot 段。显式禁止照抄内容，只借语气。"""
    if not rows:
        return ""
    lines = ["【你的说话示例】（都是你自己说过的话，照这个味道说，不要照抄内容）"]
    for r in rows:
        lines.append(f"- {str(r.get('content', '')).strip()[:80]}")
    return "\n".join(lines)


def audit_channels(
    g_active: Sequence[Dict[str, Any]],
    anchors: Sequence[Dict[str, Any]],
    r_t: Sequence[Dict[str, Any]],
) -> Dict[str, int]:
    """不变量 I2 的运行期审计：返回三处各自的 quote 泄漏计数（应为全 0）。"""
    return {
        "g_active_leaked": sum(1 for r in g_active if is_quote(r)),
        "anchors_leaked": sum(1 for r in anchors if is_quote(r)),
        "r_t_leaked": sum(1 for r in r_t if is_quote(r)),
    }
