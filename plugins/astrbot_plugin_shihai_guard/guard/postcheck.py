"""生成后置一致性校验（论文 §18.2 第③步的落地补充）。

思想：状态层给出"该有多强"，表达层负责"说出来"。若生成文本的情绪强度
与 e(t) 明显不符，说明语言层在抢解释权——不重生成也不假装没发生，而是
降级：把强度描述替换为中性表述，并记一条 consistency 日志。

本模块只用词表与规则（极性/强度/时态线索），不调用任何模型，
因此可在毫秒级完成，不增加 LLM 成本。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# 强度线索：命中即计分（词表可扩展）
_INTENSITY = {
    "强": ("超级", "特别", "真的", "好想", "太", "疯了", "崩了", "救命", "！！", "绝对"),
    "中": ("有点", "挺", "蛮", "比较", "稍微", "一下"),
}
_PAST_RX = re.compile(r"(已经|上次|之前|那次|那天|刚才|昨天|上周|早就)")
_PRESENT_RX = re.compile(r"(现在|此刻|今天|刚刚|正在)")
_POLARITY = {
    "neg": ("难过", "难受", "委屈", "烦", "崩溃", "生气", "害怕", "焦虑", "累", "想哭"),
    "pos": ("开心", "高兴", "喜欢", "好棒", "太棒", "幸福", "期待", "谢谢"),
}


def score_text(text: str) -> Dict[str, Any]:
    t = text or ""
    strong = sum(t.count(w) for w in _INTENSITY["强"])
    mild = sum(t.count(w) for w in _INTENSITY["中"])
    neg = sum(t.count(w) for w in _POLARITY["neg"])
    pos = sum(t.count(w) for w in _POLARITY["pos"])
    intensity = 0.0
    if strong or mild:
        intensity = (1.0 * strong + 0.4 * mild) / max(1.0, strong + mild)
    polarity = "neutral"
    if neg > pos:
        polarity = "neg"
    elif pos > neg:
        polarity = "pos"
    return {
        "intensity": round(intensity, 3),
        "polarity": polarity,
        "neg_hits": neg, "pos_hits": pos,
        "past_hint": bool(_PAST_RX.search(t)),
        "present_hint": bool(_PRESENT_RX.search(t)),
        "chars": len(t),
    }


def check_against_state(
    text: str,
    e_dom: str,
    e_val: float,
    target_intensity: float,
    tol: float = 0.35,
) -> Dict[str, Any]:
    """对比文本强度与状态强度。返回是否通过、偏差与建议动作。"""
    sc = score_text(text)
    dev = abs(sc["intensity"] - float(target_intensity))
    ok = dev <= tol
    action = "pass"
    if not ok and sc["intensity"] > target_intensity:
        action = "downgrade"      # 表达强于状态：降强度
    elif not ok and sc["intensity"] < target_intensity and float(e_val) >= 0.6:
        action = "upgrade"        # 状态强于表达：至少接住情绪
    return {
        "pass": ok, "deviation": round(dev, 3), "action": action,
        "text_score": sc, "e_dom": e_dom, "e_val": round(float(e_val), 3),
        "target_intensity": round(float(target_intensity), 3),
    }


def check_temporal_claim(text: str) -> Dict[str, Any]:
    """时态/催促线索检查：已完成事项被追问是"时序错引"的直接表征。"""
    t = text or ""
    nudge = re.search(r"(好了没|做完了吗|抓紧|快去|别忘了|还没|赶紧)", t)
    return {"nudge": bool(nudge), "hit": nudge.group(0) if nudge else ""}


_DOWNGRADE_MAP = (
    ("超级", "挺"), ("特别", "比较"), ("真的", ""), ("太", "有些"),
    ("！！", "。"), ("疯了", "有点受不了"), ("崩了", "有点撑不住"),
)


def downgrade(text: str) -> str:
    """强度降级：只换词，不改意思，保持可读。"""
    out = text or ""
    for a, b in _DOWNGRADE_MAP:
        out = out.replace(a, b)
    return out
