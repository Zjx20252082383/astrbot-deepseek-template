"""零 LLM 外部情绪刺激 s_ext 估计（论文 §5.1：s_ext(x) 从用户输入 x 推断，ℝ⁶）。

设计取舍：论文允许"由 LLM 分类或语音情绪识别得到"，但每条消息多一次 LLM 调用
会翻倍延迟与 token 成本。这里用情绪词典 + 标点/表情启发式实现零成本估计，
强度按消息长度做对数式缩放；LLM 侧仍可通过 shihai_inject_emotion 工具
（或在提示词里被指令）注入更精细的刺激。
"""

from __future__ import annotations

import re
from typing import Dict

from .state import EMOTION_DIMS, clamp

# 情绪词典（子串匹配）。权值为单次命中的刺激量。
_LEXICON: Dict[str, Dict[str, float]] = {
    "happy": {
        "哈哈": 0.45, "嘿嘿": 0.40, "嘻嘻": 0.40, "开心": 0.45, "高兴": 0.45,
        "快乐": 0.40, "太好了": 0.50, "好耶": 0.50, "牛": 0.30, "厉害": 0.35,
        "恭喜": 0.45, "笑死": 0.45, "好玩": 0.30, "喜欢这个": 0.35, "赞": 0.30,
        "666": 0.30, "牛啊": 0.35, "太棒": 0.45, "awa": 0.30, "happy": 0.35,
    },
    "sad": {
        "难过": 0.50, "伤心": 0.50, "难受": 0.45, "哭": 0.40, "呜呜": 0.45,
        "委屈": 0.45, "郁闷": 0.40, "烦死了": 0.35, "累死": 0.35, "emo": 0.45,
        "不想活": 0.80, "难过死": 0.55, "想哭": 0.45, "失落": 0.40, "心酸": 0.45,
        "好累": 0.35, "可惜": 0.30, "唉": 0.25, "sad": 0.35,
    },
    "angry": {
        "生气": 0.55, "气死": 0.60, "恼火": 0.45, "可恶": 0.40, "混蛋": 0.50,
        "离谱": 0.30, "烦人": 0.40, "恶心": 0.40, "离我远点": 0.55, "闭嘴": 0.55,
        "滚": 0.60, "废物": 0.50, "傻逼": 0.55, "艹": 0.40, "气人": 0.40,
    },
    "fear": {
        "害怕": 0.55, "好怕": 0.55, "吓死": 0.50, "恐怖": 0.45, "慌": 0.40,
        "紧张": 0.40, "焦虑": 0.45, "不安": 0.40, "完蛋": 0.40, "糟了": 0.35,
        "不敢": 0.30, "担心": 0.35, "怕怕": 0.45,
    },
    "love": {
        "爱你": 0.70, "喜欢你": 0.65, "想你": 0.55, "抱抱": 0.50, "亲亲": 0.50,
        "宝贝": 0.45, "老婆": 0.45, "老公": 0.45, "心你": 0.40, "摸摸头": 0.40,
        "贴贴": 0.55, "在一起": 0.35, "陪陪我": 0.45, "晚安": 0.15, "早安": 0.15,
        "么么": 0.45, "mua": 0.40, "喜欢跟你": 0.45,
    },
    "surprise": {
        "卧槽": 0.35, "我靠": 0.35, "真的假的": 0.45, "不会吧": 0.45, "震惊": 0.55,
        "居然": 0.35, "竟然": 0.35, "没想到": 0.40, "离大谱": 0.35, "??": 0.30,
        "什么鬼": 0.40, "天哪": 0.45, "我去": 0.30, "惊了": 0.45,
    },
}

# QQ 黄脸 / 表情文字代号 → 情绪
_FACE_MAP: Dict[str, tuple[str, float]] = {
    "[呲牙]": ("happy", 0.35), "[微笑]": ("happy", 0.20), "[偷笑]": ("happy", 0.35),
    "[憨笑]": ("happy", 0.35), "[色]": ("love", 0.30), "[亲亲]": ("love", 0.45),
    "[爱心]": ("love", 0.45), "[飞吻]": ("love", 0.45), "[抱抱]": ("love", 0.45),
    "[流泪]": ("sad", 0.40), "[大哭]": ("sad", 0.50), "[委屈]": ("sad", 0.45),
    "[冷汗]": ("fear", 0.30), "[惊讶]": ("surprise", 0.40), "[吓]": ("fear", 0.45),
    "[发怒]": ("angry", 0.50), "[咒骂]": ("angry", 0.45), "[翻白眼]": ("angry", 0.30),
    "[惊喜]": ("surprise", 0.40), "[晕]": ("surprise", 0.25),
}

_EMOJI_LOVE = re.compile(r"(❤|💕|💖|💗|😘|🥰|😍|🤗)")
_EMOJI_HAPPY = re.compile(r"(😄|😆|🎉|✨|😋|👍|😆|🤣|😊)")
_EMOJI_SAD = re.compile(r"(😭|😢|😞|💔|🥺|😩)")
_EMOJI_ANGRY = re.compile(r"(😡|🤬|💢)")
_EMOJI_FEAR = re.compile(r"(😱|😰|😨|😬)")
_EMOJI_SURPRISE = re.compile(r"(😱|🤯|😳|⁉)")

_EMOJI_RES = {
    "happy": _EMOJI_HAPPY, "sad": _EMOJI_SAD, "angry": _EMOJI_ANGRY,
    "fear": _EMOJI_FEAR, "love": _EMOJI_LOVE, "surprise": _EMOJI_SURPRISE,
}


def estimate_emotion(text: str) -> Dict[str, float]:
    """文本 → ℝ⁶ 外部情绪刺激。命中越多刺激越强，单维封顶 1.0。"""
    s = {dim: 0.0 for dim in EMOTION_DIMS}
    if not text:
        return s
    low = text.lower()
    for dim, words in _LEXICON.items():
        hits = 0.0
        for w, weight in words.items():
            if w in low:
                hits += weight
        s[dim] = min(1.0, hits)
    for face, (dim, weight) in _FACE_MAP.items():
        if face in text:
            s[dim] = min(1.0, s[dim] + weight)
    for dim, rx in _EMOJI_RES.items():
        n = len(rx.findall(text))
        if n:
            s[dim] = min(1.0, s[dim] + 0.30 * n)
    # 标点启发式：感叹号→开心/惊讶，连续问号→惊讶
    bang = text.count("！") + text.count("!")
    if bang >= 1:
        s["happy"] = min(1.0, s["happy"] + 0.10 * min(bang, 3))
        s["surprise"] = min(1.0, s["surprise"] + 0.06 * min(bang, 3))
    qm = text.count("？") + text.count("?")
    if qm >= 2:
        s["surprise"] = min(1.0, s["surprise"] + 0.12)
    # 长度缩放：短消息刺激轻，长倾诉刺激强（对数式，1-1/e 封顶）
    scale = 0.55 + 0.45 * (1.0 - pow(2.718281828, -len(text) / 60.0))
    return {dim: clamp(v * scale, 0.0, 1.0) for dim, v in s.items()}
