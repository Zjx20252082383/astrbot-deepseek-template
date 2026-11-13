"""情绪相对基线与语气映射（本地计算，零 Token）。

为什么需要它：

    情绪场方程 τ·de/dt = −e + κ·(s_ext + s_int) + η·ΣW·e
    稳态近似 e ≈ κ·s_ext，κ=0.8。

而 `s_ext` 来自 `classify.estimate_emotion` 的词表命中累加，日常聊天里
命中的权重多在 0.05–0.2，于是 e 常年停在 0.04–0.08。

这不是 bug，是输入刺激本来就弱。后果是：任何用绝对阈值判断"她此刻开心吗"
的设计都会全线失效——语音抽不到、语片加不出 emotion、意图包永远显示"开心4%"。

所以改看她自己的历史：这一次数值比平时高出多少个标准差。
0.042 若是平时的 1 倍标准差以上，那就是"今天不一样"，够用来驱动表达。

纯函数 + 一个滚动历史文件（200 条封顶），不依赖 AstrBot，可离线单测。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Any, Dict, List

KEEP = 200        # 滑动窗口：只留最近 200 条，相处久了也不会变迟钝
ALPHA = 0.12      # 指数平滑系数（越小越稳，越大越跟手）
MIN_SD = 0.012    # 标准差下限，避免历史太平导致 z 爆炸

# 冷启动：前 COLD_N 条还没有可信的历史，此时不能拿 3 条样本算标准差——
# 否则一条稍强的消息就能让 z 飙到 3.0，语音立刻误触发。
# 先用一组固定基线（mean=0.05 / sd=0.02，取自真实数据的长期中枢），
# 攒够样本再切动态。
COLD_N = 20
COLD_MEAN = 0.05
COLD_SD = 0.02

# 情绪维度 → 中文
E_CN = {"happy": "开心", "sad": "难过", "angry": "生气",
        "fear": "害怕", "love": "依恋", "surprise": "惊讶"}

# (主导情绪, 强度档) → 语音语气描述。
# 这段话直接进 TTS 的演出提示，所以必须是"怎么说"而不是"什么情绪"——
# 「开心」这种标签喂给 TTS 没用，「带笑、尾音上扬」才有用。
VOICE_TONE: Dict[tuple, str] = {
    ("happy", "strong"): "开心带笑，语速偏快，尾音上扬",
    ("happy", "mid"):    "轻松，带着一点笑意",
    ("happy", "weak"):   "平静里透点愉快",
    ("sad", "strong"):   "低落，声音有点闷，带一声叹息",
    ("sad", "mid"):      "心情不太好，语速慢下来",
    ("sad", "weak"):     "淡淡的，不提不起劲",
    ("angry", "strong"): "有点冲，语速快，尾音短促",
    ("angry", "mid"):    "不太高兴，语气硬一点",
    ("love", "strong"):  "很软，语速慢，像在耳边说",
    ("love", "mid"):     "温和亲近，带点撒娇",
    ("fear", "strong"):  "有点慌，声音发紧",
    ("surprise", "strong"): "惊讶，语调扬起",
}
TONE_DEFAULT = "自然说话，语速和平时一样"


# ---------- 持久化 ----------

def _load(path: str) -> List[float]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data = data.get("hist", [])
        return [float(x) for x in data if isinstance(x, (int, float))]
    except (OSError, ValueError, TypeError):
        return []


def _save(path: str, hist: List[float]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"hist": hist[-KEEP:]}, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------- 纯计算 ----------

def mean(hist: List[float], alpha: float = ALPHA) -> float:
    """指数平滑均值。最近的值权重更高，但不至于被单次尖峰带跑。"""
    if not hist:
        return 0.0
    m = hist[0]
    for x in hist[1:]:
        m = alpha * x + (1.0 - alpha) * m
    return m


def std(hist: List[float], alpha: float = ALPHA) -> float:
    if len(hist) < 3:
        return MIN_SD
    m = mean(hist, alpha)
    var = 0.0
    w = 1.0
    tot = 0.0
    for x in reversed(hist):
        var += w * (x - m) ** 2
        tot += w
        w *= (1.0 - alpha)
    return max(math.sqrt(var / max(tot, 1e-9)), MIN_SD)


def zscore(hist: List[float], value: float) -> float:
    """这次比平时高/低多少个标准差。

    样本不足 COLD_N 时用固定基线，不拿小样本估方差 —— 三条记录算出来的
    标准差可能只有 0.01，一条普通消息就能算出 z=3，语音会当场误触发。
    """
    v = float(value)
    if len(hist) < COLD_N:
        return (v - COLD_MEAN) / COLD_SD
    return (v - mean(hist)) / std(hist)


def level(z: float) -> str:
    """z → 强度档。用于语气映射与语片判断。"""
    if abs(z) >= 1.5:
        return "strong"
    if abs(z) >= 0.6:
        return "mid"
    return "weak"


# ---------- 对外入口 ----------

class EmotionBaseline:
    """一个薄壳，把"记录 + 打分"包成对象，调用方不用管文件细节。"""

    def __init__(self, path: str):
        self.path = path
        self.hist = _load(path)

    def observe(self, e_dom_value: float) -> float:
        """记一次采样，返回这次相对历史的 z。"""
        v = float(e_dom_value or 0.0)
        z = zscore(self.hist, v)
        self.hist.append(v)
        if len(self.hist) > KEEP * 2:
            self.hist = self.hist[-KEEP:]
        _save(self.path, self.hist)
        return z

    def peek(self, e_dom_value: float) -> float:
        """只看不打点（注入渲染用，避免每轮都写盘）。"""
        return zscore(self.hist, float(e_dom_value or 0.0))

    def tone(self, e_dom: str, e_dom_value: float) -> str:
        """当前该用什么语气说话。本地算，不问 LLM。"""
        z = self.peek(e_dom_value)
        lv = level(z)
        # 负向偏离（比平时还低）时不套用正向语气
        if z < -0.6:
            return VOICE_TONE.get(("sad", "mid"), TONE_DEFAULT)
        return VOICE_TONE.get((e_dom, lv), TONE_DEFAULT)

    def label(self, e_dom: str, e_dom_value: float) -> str:
        """给提示词用的一行：不是百分比，是"比平时高/低"。"""
        z = self.peek(e_dom_value)
        name = E_CN.get(e_dom, e_dom)
        if z >= 1.5:
            return f"{name}，比平时明显高"
        if z >= 0.6:
            return f"{name}，比平时高一些"
        if z <= -0.6:
            return f"偏平静，{name}比平时还低"
        return f"{name}，和平时差不多"


def tone_of(e_dom: str, e_dom_value: float, baseline: Any = None) -> str:
    """无状态兜底：拿不到基线时给一条中性语气，不要编造。"""
    if baseline is not None:
        try:
            return baseline.tone(e_dom, e_dom_value)
        except Exception:  # noqa: BLE001
            pass
    return TONE_DEFAULT
