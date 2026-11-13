"""涌现行为空间（论文 §12 的可执行注册表；当前注册 13 条，另有 46 个 LLM 工具）。

涌现接口：behaviors.json 是行为注册表的唯一真源（首次运行时从默认表写出），
每个行为声明自己的触发条件（requires：情绪阈值 / 目标阈值 / 可用锚点），
行为由动力学状态"自然涌现"——状态满足条件 + π 打分最高者被选中。
增删行为、调触发阈值 = 编辑 behaviors.json 或调用 behavior_tune 工具，无需改代码。

needs 字段 = 执行依赖的适配器（论文 §13.1 Platform Adapter）：
    ""          行为插件自己就能完成（生成一句话发给主人）
    "ai_cover"  需要 RVC 翻唱服务（v1 不自动触发，留作涌现候选）
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

# 锚点类型（核心 fire_pending.anchors[].kind）→ 缺省行为映射

# 说话形态（本地决定"该说什么"，LLM 只负责把这一句填出来）。
#
# 为什么必须分：四个主动来源不能等价。冷落抬升是"她有点想说话"，承诺临近是
# "有件事必须说清楚"，自发漂移是"想分享个东西"。如果不分，就会出这种场面——
# 她自己无聊了，突然严肃提醒你交材料。这是人格分裂，不是个性。
#
# chat   : 轻量搭话，不携带任何待办/要求
# remind : 明确提醒，必须带出"什么事、什么时候"
# share  : 分享手上的东西，不借机提要求
SPEECH_KIND: Dict[str, str] = {
    "proactive_greeting": "chat",
    "care_inquiry":       "chat",
    "proactive_chat":     "chat",
    "recall_share":       "share",
    "promise_followup":   "remind",
    "sing_cover":         "share",
    "draw_share":         "share",
    "post_feed":          "share",
}


def speech_kind_of(action: Optional[Dict[str, Any]]) -> str:
    """取行为的说话形态。注册表里没写就按 id 查表，再退 chat。"""
    if not action:
        return "chat"
    explicit = str(action.get("speech_kind") or "").strip()
    if explicit in ("chat", "remind", "share"):
        return explicit
    return SPEECH_KIND.get(str(action.get("id", "")), "chat")


DEFAULT_BEHAVIORS: List[Dict[str, Any]] = [
    {
        "id": "proactive_greeting", "label": "主动打招呼", "category": "主动行为",
        "intent_type": "主动关心", "proactive": True, "needs": "",
        "phi": {"g_relation": 0.5, "g_avoid": -0.1},
        "requires": {}, "anchor": ["will", "recent"], "enabled": True,
    },
    {
        "id": "care_inquiry", "label": "关心近况", "category": "关系行为",
        "intent_type": "主动关心", "proactive": True, "needs": "",
        "phi": {"g_relation": 0.7, "g_avoid": 0.2},
        "requires": {}, "anchor": ["will", "recent"], "enabled": True,
    },
    {
        "id": "proactive_chat", "label": "主动闲聊", "category": "对话与表达",
        "intent_type": "主动关心", "proactive": True, "needs": "",
        "phi": {"g_relation": 0.85, "g_self": 0.2},
        "requires": {}, "anchor": ["recent", "will", "revisit"], "enabled": True,
    },
    {
        "id": "recall_share", "label": "分享想起的旧事", "category": "主动行为",
        "intent_type": "主动回忆", "proactive": True, "needs": "",
        "phi": {"g_memory": 0.5, "g_relation": 0.3},
        "requires": {}, "anchor": ["revisit"], "enabled": True,
    },
    {
        "id": "promise_followup", "label": "承诺跟进", "category": "主动行为",
        "intent_type": "主动回忆", "proactive": True, "needs": "",
        "phi": {"g_relation": 0.6, "g_memory": 0.3},
        "requires": {}, "anchor": ["promise"], "enabled": True,
    },
    {
        "id": "sing_cover", "label": "唱歌给主人听", "category": "对话与表达",
        "intent_type": "主动关心", "proactive": True, "needs": "ai_cover",
        "phi": {"g_relation": 0.4, "g_self": 0.3},
        "requires": {}, "anchor": ["will", "recent", "revisit"], "enabled": True,
    },
    {
        "id": "draw_share", "label": "画一张给你", "category": "自我表露",
        "intent_type": "主动关心", "proactive": True, "needs": "draw",
        "phi": {"g_self": 0.35, "g_relation": 0.2},
        "requires": {}, "anchor": ["will", "recent"], "enabled": True,
    },
    # —— 被动/后台行为：登记在册供 π 观测与 Q 反馈，v1 不由看门狗自动触发 ——
    {"id": "chat_followup", "label": "接续话题", "category": "对话与表达",
     "intent_type": "被动应答", "proactive": False, "needs": "",
     "phi": {"g_relation": 0.6, "g_self": 0.1}, "requires": {}, "enabled": True},
    {"id": "sticker", "label": "发表情包", "category": "对话与表达",
     "intent_type": "被动应答", "proactive": False, "needs": "dafeiyu",
     "phi": {"g_relation": 0.3, "g_self": 0.2}, "requires": {}, "enabled": True},
    {"id": "diary_write", "label": "写日记", "category": "自我表露",
     "intent_type": "被动应答", "proactive": False, "needs": "dafeiyu",
     "phi": {"g_memory": 0.6, "g_self": 0.3}, "requires": {}, "enabled": True},
    {"id": "post_feed", "label": "自己发说说", "category": "自我表露",
     "intent_type": "主动关心", "proactive": True, "needs": "qzone",
     "phi": {"g_self": 0.5, "g_relation": 0.2}, "requires": {},
     "anchor": ["will", "recent", "revisit"], "enabled": True},
    {"id": "set_signature", "label": "改签名", "category": "自我表露",
     "intent_type": "被动应答", "proactive": False, "needs": "dafeiyu",
     "phi": {"g_self": 0.6}, "requires": {}, "enabled": True},
    {"id": "dream_consolidate", "label": "梦境固化", "category": "后台行为",
     "intent_type": "被动应答", "proactive": False, "needs": "",
     "phi": {"g_memory": 0.9}, "requires": {}, "enabled": True},
]


def load_registry(path: str) -> List[Dict[str, Any]]:
    """加载 behaviors.json；不存在则从默认表写出。坏文件回退默认表。

    老注册表没有 speech_kind 字段，读出来时按 id 补齐，不用手改 json。
    """
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            rows = data.get("behaviors", []) if isinstance(data, dict) else data
            if isinstance(rows, list) and rows:
                for r in rows:
                    if isinstance(r, dict) and "speech_kind" not in r:
                        r["speech_kind"] = SPEECH_KIND.get(str(r.get("id", "")), "chat")
                return rows
        except (OSError, ValueError):
            pass
    save_registry(path, DEFAULT_BEHAVIORS)
    return [dict(b) for b in DEFAULT_BEHAVIORS]


def save_registry(path: str, behaviors: List[Dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"behaviors": behaviors}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def check_requires(action: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """涌现条件：行为的行为由状态决定（requires 全部满足才进入候选）。"""
    req = action.get("requires") or {}
    emo_req = req.get("emotion") or {}
    e = state.get("e", {}) or {}
    for dim, th in emo_req.items():
        if float(e.get(dim, 0.0)) < float(th):
            return False
    goal_req = req.get("goal") or {}
    g = state.get("goals", {}) or {}
    for gid, th in goal_req.items():
        if float(g.get(gid, 0.0)) < float(th):
            return False
    return True
