"""表达归界（论文 §18）：性格层 S 只谈风格，心情层 M 只谈状态。

不变量 I3（表达归界）：
    S ∩ 状态断言 = ∅      —— 提示词里不出现"你现在…/总是…/绝不…"式状态与强度断言；
    M ∩ 身份与风格 = ∅    —— 意图包只携带状态量，不携带姓名、关系设定、文体规则。

实现方式：S 不写死形容词，而由 ψ（大五人格）经单调映射渲染成"表达参数"，
每次生成按当前 ψ 重算。这样性格只有一个事实源，且随月级漂移而变。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# 绝对化状态词：出现在 S 中即视为"静态断言"，即 I3 违规。
ABSOLUTE_MARKERS = (
    "总是", "从不", "绝不", "永远", "一直", "必定", "绝对不", "从来不",
    "你现在", "此刻心情", "今天心情",
)

_STATE_RX = re.compile("|".join(ABSOLUTE_MARKERS))


# 否定语境豁免：命中词紧跟在纪律动词后时是"纪律命令"而非"人格断言"，
# 如"绝不要追问'好了没'"是对模型的约束，不算 I3 违规
_NEGATION_RX = re.compile(r"(禁止|不要|不得|不允许|绝不要|别|不得再|切勿)[^\n。]{0,6}$")

# "绝不+动词"是行为纪律，不是状态断言：
#   "绝不当冷淡/绝不许复制/绝不说不会/绝不抢戏/绝不允许编造"
# 只有"绝不+情绪形容词"（绝不生气/绝不难过）才按人格断言处理。
_DISCIPLINE_AFTER = set("当许说抢允要准能会得让把再可")


def _is_discipline(marker: str, text: str, end: int) -> bool:
    if marker not in ("绝不", "绝对不"):
        return False
    nxt = text[end:end + 1]
    if not nxt:
        return False
    # "绝不要…"整体是纪律命令（保留原规则）
    if nxt == "要":
        return True
    return nxt in _DISCIPLINE_AFTER


def scan_persona(text: str) -> Dict[str, Any]:
    """S 自检：返回命中的静态断言及其上下文片段（供改写，不自动改）。"""
    hits: List[Dict[str, str]] = []
    t = text or ""
    for m in _STATE_RX.finditer(t):
        if _is_discipline(m.group(0), t, m.end()):
            continue
        head = t[max(0, m.start() - 10):m.start()]
        if _NEGATION_RX.search(head):
            continue  # 纪律语境豁免
        s = max(0, m.start() - 12)
        e = min(len(t), m.end() + 12)
        hits.append({"marker": m.group(0), "span": f"{m.start()}-{m.end()}",
                     "context": t[s:e].replace("\n", " ")})
    return {"violations": len(hits), "hits": hits, "pass": len(hits) == 0}


def style_params(psi: Dict[str, float]) -> Dict[str, float]:
    """ψ → 表达参数（单调、有界）。返回的是"怎么说"的参数，不含"什么心情"。

    映射口径（论文 18.2 的工程实现）：
      E 外向 → 句长偏短、感叹与主动语气偏多；
      N 神经质 → 语气谨慎度、负向词敏感度；
      A 宜人 → 让步/共情句式比例；
      C 尽责 → 话题保持度（少跑题）；
      O 开放 → 比喻与联想密度。
    """
    def g(k: str, d: float = 0.5) -> float:
        try:
            return max(0.0, min(1.0, float(psi.get(k, d))))
        except (TypeError, ValueError):
            return d

    E, N, A, C, O = g("E"), g("N"), g("A"), g("C"), g("O")
    return {
        "sentence_len": round(1.0 - 0.5 * E, 3),        # 1.0 长句 … 0.5 短句
        "exclaim": round(0.15 + 0.60 * E, 3),           # 感叹号密度
        "initiative": round(0.20 + 0.70 * E, 3),        # 主动语气（不等于开口许可）
        "caution": round(0.20 + 0.70 * N, 3),           # 谨慎度
        "neg_sensitivity": round(0.20 + 0.70 * N, 3),   # 对负面词的敏感度
        "empathy": round(0.20 + 0.70 * A, 3),           # 共情句式比例
        "topic_hold": round(0.25 + 0.70 * C, 3),        # 话题保持度
        "metaphor": round(0.10 + 0.60 * O, 3),          # 比喻密度
    }


STYLE_RUBRIC = {
    "sentence_len": "句长：数值越小句子越短（0.5 为短句档，1.0 为长句档）",
    "exclaim": "感叹号与语气助词密度",
    "initiative": "主动语气（只影响措辞，是否开口由 r_release 决定）",
    "caution": "谨慎度：多用“可能/也许/要不要”，少下断言",
    "neg_sensitivity": "对负面词的敏感度：高则先接情绪再谈事",
    "empathy": "共情句式比例：先复述对方感受，再给内容",
    "topic_hold": "话题保持度：低则允许跳题，高则围绕当前话题",
    "metaphor": "比喻与联想密度",
}


def render_style_block(psi: Dict[str, float]) -> str:
    """把表达参数渲染成提示词可用的段落（只描述"怎么说"）。"""
    p = style_params(psi)
    lines = ["【表达参数】（由当前人格 ψ 渲染，只规定说话方式，不规定情绪）"]
    for k, v in p.items():
        lines.append(f"- {STYLE_RUBRIC.get(k, k)}：{v}")
    return "\n".join(lines)


def check_m_fields(packet: Dict[str, Any]) -> Dict[str, Any]:
    """M 自检：意图包字段不得混入身份/风格信息。"""
    banned = ("name", "identity", "persona", "style", "tone", "关系设定", "姓名")
    bad = [k for k in packet.keys() if any(b in str(k).lower() for b in banned)]
    # 值层面只查显式身份串，不做语义判断（避免引入模型）
    bad_vals = [k for k, v in packet.items()
                if isinstance(v, str) and ("我是" in v and "机器人" in v)]
    return {"bad_keys": bad, "bad_values": bad_vals,
            "pass": not bad and not bad_vals}


def arbitrate(state_view: Dict[str, Any], style_view: Dict[str, Any]) -> Dict[str, Any]:
    """不变量 I4（解释权唯一）：冲突时以状态派生字段为准，风格只做修饰。

    返回合并后的生成约束；state_view 提供 emotion/release/os，
    style_view 提供 style_params。二者键空间不相交，冲突即报错。
    """
    overlap = set(state_view) & set(style_view)
    if overlap:
        raise ValueError(f"表达层字段重叠，违反 I4：{sorted(overlap)}")
    merged = dict(style_view)
    merged.update(state_view)
    return merged
