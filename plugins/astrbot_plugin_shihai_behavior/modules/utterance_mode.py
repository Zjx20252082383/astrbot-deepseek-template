"""语片模式：本地决定"这一轮用编排，还是放开让她整段说"。

## 为什么必须有 free

固定编排（先接住 → 中间说事 → 轻收尾）在闲聊里很自然，但在"关心"场景
会把最重要的东西压掉：**"我去给你倒杯热水"不是收尾，是内容。**

把它当 transition 处理，就会得到"永远礼貌、永远不做实事"的小满。

这个判断不能交给 LLM——它读到"最后一句轻收"这条更结构化的指令，会优先
服从结构，而不是服从那句补上去的"例外"。

## 本地判定

- 情绪显著（|z| ≥ 1.5）→ 她此刻不"编辑"自己，直说
- 对方在难受 / 有事要办（comforting）→ 直说
- 有时限的事（urgency 高）→ 直说
其余走编排。

## 一个被澄清的事实

分段发送的正则是 `.*?[。？！~…]+|.+$`，**只按句末标点切，不按逗号切**。
所以"我去给你倒杯热水，你先躺着别动"不会在逗号处断开。

真正要防的是 LLM 自己把它拆成三句——那才是"节奏散了"。free 模式里
明确写"这件事说成一句"，而不是靠框架兜。
"""

from __future__ import annotations

FREE_THRESHOLD = 1.5      # |z| 超过这个档位就不再"编辑"自己
URGENCY_THRESHOLD = 0.7   # 有时限的事，优先讲清楚

FREE_RULE = (
    "【本轮不用分条】对方此刻不需要你熨帖的节奏，需要你把话说完。\n"
    "把该说的整段说出来，不要拆成三条。\n"
    "其中「我去给你倒杯热水」「我记下了」「我盯着这事」这类你要做的事，"
    "是内容本身，必须说出来，不能说成一句轻飘的收尾、也不能省掉。\n"
    "写的时候注意：一件连续的动作说成一句，别自己把它拆开。"
)

EDIT_RULE = (
    "【分条说话】1-3 句，每句只干一件事：\n"
    "第一句接住对方；要说的事放中间；最后一句轻收。\n"
    "例外：对方明显在难受、或有件事有时限必须讲清楚时，改整段说完，"
    "其中你要做的事是内容不是收尾。"
)


def mode_for(e_z: float = 0.0, urgency: float = 0.0,
             comforting: bool = False) -> str:
    """返回 "free" 或 "edit"。"""
    try:
        z = abs(float(e_z))
    except (TypeError, ValueError):
        z = 0.0
    try:
        u = float(urgency)
    except (TypeError, ValueError):
        u = 0.0
    if comforting or z >= FREE_THRESHOLD or u >= URGENCY_THRESHOLD:
        return "free"
    return "edit"


def rule_for(mode: str) -> str:
    return FREE_RULE if str(mode) == "free" else EDIT_RULE


def looks_comforting(text: str) -> bool:
    """轻量判定：对方是不是在说自己难受。

    只做粗筛——它的作用是把"可能关心"的场景往 free 推，宁可多推。
    漏推一次就是一次"没倒水"，多推一次只是少分一条。
    """
    s = str(text or "")
    if not s:
        return False
    keys = ("疼", "难受", "不舒服", "发烧", "感冒", "累", "睡不着",
            "哭了", "烦", "焦虑", "压力", "加班", "赶", "来不及",
            "病了", "头晕", "胃", "牙", "吐", "失眠")
    return any(k in s for k in keys)


# 话没说完的收尾词。对方这条以它们结尾，说明下一步还有话。
_TRAILING_OPEN = ("不过", "但是", "而且", "还有", "然后", "就是", "所以",
                  "因为", "虽然", "我想", "我觉得", "其实", "对了")

HOLD_GAP_SEC = 8.0       # 距上一条太近，插话像抢
HOLD_BURST_MSGS = 2      # 90 秒内已连发这么多条，说明还在说


def should_hold(msgs_in_90s: int, seconds_since_prev: float = 999.0,
                last_text: str = "") -> bool:
    """现在该不该闭嘴等对方说完。True = 先别说。

    用在**主动开口**之前：她自己在群聊里插一句「我去给你倒杯热水」，
    如果对方正连着打字，这句就从「关心」变成「抢话」。

    被动回复不走这个判断 —— 用户说完才回，天然不抢。
    """
    try:
        n = int(msgs_in_90s)
    except (TypeError, ValueError):
        n = 0
    try:
        gap = float(seconds_since_prev)
    except (TypeError, ValueError):
        gap = 999.0
    s = str(last_text or "").rstrip(" \t\n。！？!?~…，,、")
    if s.endswith(_TRAILING_OPEN):
        return True
    if n >= HOLD_BURST_MSGS and gap < HOLD_GAP_SEC:
        return True
    return False
