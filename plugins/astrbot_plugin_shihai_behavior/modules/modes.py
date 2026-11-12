"""身份 → 权限的三层判定（不是"双姿态打分"，那是我上一版的错误）。

上一版按"这句话像办事还是像撒娇"连续打分再回转，两个问题：
1. 身份是可核验的离散事实（sender_id == OWNER_QID 要么成立要么不成立），
   给它套 σ / EMA 是造轮子，还会引入三个待调参数和"0.79 是不是主人"这种
   没有意义的中间态；
2. 用"话题"决定"姿态"，会让人格随话题漂移——同一句话她一会儿客服一会儿
   撒娇，这才是精神分裂。

所以这里只做一件事：
    mode = if sender_id == OWNER_QID → companion
           elif sender_id 在熟人名单 → assistant
           else → restricted

σ 只留给 R_o（关系强度），因为"有多熟"确实是连续量；"是谁"不是。

模式切换的是**权限**（能看到多少记忆、能做哪些操作、涉及别人私事时含糊
到什么程度），**不是人格**。小满跟谁都还是小满：她跟朋友也会撒娇，只是
被问到鑫鑫的私事时"她自己决定不细说"，而不是切成客服腔。
"""
from __future__ import annotations

from typing import Iterable, Optional, Set

MODE_COMPANION = "companion"
MODE_ASSISTANT = "assistant"
MODE_RESTRICTED = "restricted"

# 三档权限表（代码是唯一真相，文档只是它的影子）
POLICY = {
    MODE_COMPANION: {"memory": "all", "tools": "all", "discretion": "none"},
    MODE_ASSISTANT: {"memory": "shared", "tools": "limited", "discretion": "half"},
    MODE_RESTRICTED: {"memory": "public", "tools": "query", "discretion": "full"},
}

# 给模型的**权限**提示：只讲"能看到/能做/要不要含糊"，一句人格风格的话都没有。
_HINT = {
    MODE_COMPANION: (
        "【这一轮的对象是鑫鑫本人】记忆、工具、私事都不必设防，说话方式还是你平时那样。"
        "记不清某句话当初怎么说的，就用 recall_transcript 回查原话，不要拿「大概」「好像」糊过去。"
        "他让你「到时候提醒我」并说了具体时间（明天8点／周三下午／11月15日）时，"
        "你必须调用 remember_commitment 记账——只在嘴上说「记住了」不算记账，"
        "到点你不会想起来；时间含糊（改天、回头）时不许记账，"
        "必须反问一句「大概是哪天」。"
        "已经记下的账，到点由你自己主动开口，不用他再提一次。"
        "括号里只写你自己此刻的内在状态（语气、心情、语速），不要写需要外部事实才能成立的动作或遭遇"
        "（比如「说到一半被打断」「正好路过」）；没被告知的事，就是没发生。"),
    MODE_ASSISTANT: (
        "【这一轮的对象是认识的人】你们共同的经历可以聊；鑫鑫的私事（日程、情绪、"
        "家人、他在别处说过的话）不细讲——用你自己的口气岔开或含糊过去"
        "（比如「他最近有点忙啦，具体我就不多说啦~」），不要提隐私、政策、权限这类词。"),
    MODE_RESTRICTED: (
        "【这一轮的对象是不认识的人】只答你自己知道、也不涉及任何具体人的事；"
        "问到鑫鑫或别人的私事，就用你自己的口气轻轻带过、把话岔回对方身上，"
        "不要解释你为什么不能说。"),
}


def mode_for(sender_id: object, owner_qid: object,
             known: Optional[Iterable[object]] = None) -> str:
    """三层 if-else：零参数、零调参、可回滚。"""
    sid = str(sender_id or "").strip()
    if sid and sid == str(owner_qid or "").strip():
        return MODE_COMPANION
    if known:
        kset: Set[str] = {str(x).strip() for x in known if str(x or "").strip()}
        if sid and sid in kset:
            return MODE_ASSISTANT
    return MODE_RESTRICTED


def hint(mode: str) -> str:
    return _HINT.get(str(mode or ""), "")


def policy(mode: str) -> dict:
    return dict(POLICY.get(str(mode or ""), POLICY[MODE_RESTRICTED]))


def memory_tag(sender_id: object, owner_qid: object, origin: str = "") -> str:
    """归属 tag：owner 层=""、私聊=qq:<号>、群=group:<群号>。与 memory_v2 口径一致。"""
    sid = str(sender_id or "").strip()
    if sid and sid == str(owner_qid or "").strip():
        return "owner"
    org = str(origin or "")
    grp = ""
    if "GroupMessage" in org or "group" in org.lower():
        try:
            grp = org.split(":")[-1]
        except Exception:  # noqa: BLE001
            grp = ""
    if grp and grp.isdigit():
        return f"group:{grp}"
    return f"qq:{sid}" if sid else "owner"
