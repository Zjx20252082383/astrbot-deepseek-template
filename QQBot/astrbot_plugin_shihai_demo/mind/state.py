"""z(t) 状态容器与持久化。

论文 §4 状态空间：
    z(t) = [ e(t)∈ℝ⁶,  a(v,t)∈ℝ^|V|,  θ(t)∈ℝ^d,  ψ∈ℝ⁵,  u(t)∈ℝ,  g(t)∈ℝ⁴ ]

- e     : 六维情绪场 (happy, sad, angry, fear, love, surprise)，取值 [0,1]
- a     : 图谱节点激活值（本体存于 MemoryGraph，这里只留最近召回摘要）
- θ     : 慢演化参数（开口阈值 / 发脾气阈值 / 依恋 / 自我效能）
- ψ     : 大五人格 (O, C, E, A, N)
- u     : 念头强度
- g     : 内生目标向量 (g_relation, g_memory, g_self, g_avoid)

落盘为 JSON，tmp + os.replace 原子写。纯 Python，不依赖 AstrBot。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict

EMOTION_DIMS = ("happy", "sad", "angry", "fear", "love", "surprise")

# θ(t) 初值：fire_threshold=论文§9 FIRE_THRESHOLD；其余见 §8/§11
DEFAULT_THETA: Dict[str, float] = {
    "fire_threshold": 2.10,     # 念头攒到此值才主动开口
    "tantrum_threshold": 0.60,  # θ_tantrum 基准：angry 超过它才可能发脾气（§8：×(1+A)）
    "send_threshold": 1.00,     # 依恋 att：用户回复一致性调制的主动开口频率（§8）
    "eff": 0.60,                # 自我效能：成功 +0.05 / 失败 -0.1（§8）
}

DEFAULT_PSI: Dict[str, float] = {
    "O": 0.55, "C": 0.45, "E": 0.60, "A": 0.70, "N": 0.35,
}

DEFAULT_GOALS: Dict[str, float] = {
    "g_relation": 0.60, "g_memory": 0.30, "g_self": 0.20, "g_avoid": 0.40,
}


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class MindState:
    """可持久化的 z(t)。graph 的 a(v,t) 不在这里，见 MemoryGraph。"""

    def __init__(self, path: str):
        self.path = path
        self.e: Dict[str, float] = {k: 0.0 for k in EMOTION_DIMS}
        self.theta: Dict[str, float] = dict(DEFAULT_THETA)
        self.psi: Dict[str, float] = dict(DEFAULT_PSI)
        self.goals: Dict[str, float] = dict(DEFAULT_GOALS)
        self.q_table: Dict[str, float] = {}
        self.urge: float = 0.0            # u(t)
        self.streak_gate: float = 0.0     # 被冷落后抬高的门槛（STREAK_GATE_STEP 累积）
        self.suppress_until: float = 0.0  # "别老找我"之后压制主动开口到这个时刻
        # —— §5.10 EgoRegulator 的输入信号（C = (conflict_flag, streak)）——
        self.r_feedback: float = 1.0      # 最近社交反馈标量 ∈[0,1]（§5.8 映射，adapt 写入）
        self.conflict_steps: int = 0      # 冲突窗口剩余决策步（§5.10.2(b)：g_avoid≥0.6 时 3 步）
        self.streak_count: int = 0        # 连续未回复计数（§5.10.1 分段表用）
        self.ego_trace: list = []         # §17.1(iii) 回流/决策审计日志
        # 念头攒满时核心写下的开火指令（Behavior API，论文§13.1）：
        # 核心只发"该说话了+锚点候选"，说什么、怎么发由行为插件（shihai_behavior）决定
        self.fire_pending: Dict[str, Any] = {}
        self.last_user_msg_at: float = 0.0
        self.last_bot_proactive_at: float = 0.0
        self.last_proactive_text: str = ""
        self.last_proactive_action: str = ""
        self.last_proactive_replied: bool = True
        # 上一次主动开口的"决定档案"（先于行动写入，供自我叙事引用）：
        # {intent_id, reason, self_view, action, text, u, ts}
        self.last_decision: Dict[str, Any] = {}
        self.last_dream_date: str = ""
        self.updated_at: float = 0.0
        self._last_observe_at: float = 0.0  # 上次外部刺激注入时刻（情绪子步长用）
        if os.path.exists(self.path):
            try:
                self._load()
            except Exception:
                # 损坏时保留默认值，不阻断心智核启动
                pass

    # ---------- 持久化 ----------

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for dim in EMOTION_DIMS:
            self.e[dim] = clamp(float(data.get("e", {}).get(dim, 0.0)), 0.0, 1.0)
        for group, target in (("theta", self.theta), ("psi", self.psi),
                              ("goals", self.goals)):
            src = data.get(group, {}) or {}
            for key in list(target):
                try:
                    target[key] = float(src.get(key, target[key]))
                except (TypeError, ValueError):
                    pass
        self.q_table = {str(k): float(v)
                        for k, v in (data.get("q_table", {}) or {}).items()
                        if isinstance(v, (int, float))}
        self.urge = clamp(float(data.get("urge", 0.0)), 0.0, 10.0)
        self.streak_gate = clamp(float(data.get("streak_gate", 0.0)), 0.0, 6.0)
        self.suppress_until = float(data.get("suppress_until", 0.0))
        self.r_feedback = clamp(float(data.get("r_feedback", 1.0)), 0.0, 1.0)
        self.conflict_steps = int(clamp(float(data.get("conflict_steps", 0)), 0, 10))
        self.streak_count = int(clamp(float(data.get("streak_count", 0)), 0, 99))
        tr = data.get("ego_trace", [])
        self.ego_trace = tr if isinstance(tr, list) else []
        fp = data.get("fire_pending", {})
        self.fire_pending = fp if isinstance(fp, dict) else {}
        self.last_user_msg_at = float(data.get("last_user_msg_at", 0.0))
        self.last_bot_proactive_at = float(data.get("last_bot_proactive_at", 0.0))
        self.last_proactive_text = str(data.get("last_proactive_text", ""))
        self.last_proactive_action = str(data.get("last_proactive_action", ""))
        self.last_proactive_replied = bool(data.get("last_proactive_replied", True))
        ld = data.get("last_decision", {})
        self.last_decision = ld if isinstance(ld, dict) else {}
        self.last_dream_date = str(data.get("last_dream_date", ""))
        self.updated_at = float(data.get("updated_at", 0.0))

    def save(self) -> None:
        self.updated_at = time.time()
        data = self.snapshot()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def snapshot(self) -> Dict[str, Any]:
        return {
            "e": dict(self.e),
            "e_dom": max(self.e, key=lambda k: self.e[k]) if self.e else "happy",
            "e_dom_value": round(max(self.e.values() or [0.0]), 3),
            "theta": dict(self.theta),
            "psi": dict(self.psi),
            "goals": {k: round(v, 3) for k, v in self.goals.items()},
            "q_table": {k: round(v, 3) for k, v in self.q_table.items()},
            "urge": round(self.urge, 3),
            "streak_gate": round(self.streak_gate, 3),
            "suppress_until": self.suppress_until,
            "r_feedback": round(self.r_feedback, 2),
            "conflict_steps": self.conflict_steps,
            "streak_count": self.streak_count,
            "ego_trace": self.ego_trace[-20:],
            "fire_pending": self.fire_pending,
            "last_user_msg_at": self.last_user_msg_at,
            "last_bot_proactive_at": self.last_bot_proactive_at,
            "last_proactive_text": self.last_proactive_text,
            "last_proactive_action": self.last_proactive_action,
            "last_proactive_replied": self.last_proactive_replied,
            "last_decision": self.last_decision,
            "last_dream_date": self.last_dream_date,
            "updated_at": self.updated_at,
        }
