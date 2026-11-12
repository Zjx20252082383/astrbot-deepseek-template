"""自适应演化（论文 §5.7 自适应方程、§5.8 总损失、§11 用户反馈信号表）。

    θ_{t+1} ← θ_t - η_adapt · ∂L_total/∂θ

反馈信号 → θ 的更新（论文 §11 表，逐条实现）：
    5 分钟内主动接续对话   正反馈   FIRE_THRESHOLD↓（更主动）
    "好/棒/谢谢"          正反馈   对应动作 Q 值↑（L_adapt）
    消息发出后 >2h 无回复  负反馈   FIRE_THRESHOLD↑（降低频率）
    "嗯/哦" 短回复         负反馈   θ_tantrum↓（收敛情绪）
    "别老找我"            强负反馈  u 增速↓（suppress），timing_fit 门槛↑

数值自洽性检查报告 §9 的修补：阈值带硬饱和上限 3.5——连续负反馈不会让系统
永久沉默；恢复正常交互后门槛逐步回落（streak_gate 衰减）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .state import MindState, clamp

_POS_WORDS = ("好", "棒", "谢谢", "厉害", "爱你", "喜欢你", "哈哈", "妙",
              "不错", "辛苦", "么么", "抱抱", "喜欢你", "太强了", "真乖")
_STRONG_NEG = ("别老找我", "别烦我", "别找我", "走开", "闭嘴", "烦不烦",
               "讨厌死了", "滚", "别说话", "理你个头")
_SHORT_REPLIES = ("嗯", "哦", "噢", "喔", "嗯嗯", "哦哦", "好", "行", "6",
                  "ok", "嗯呐", "噢噢", "啊这", "……", "。")


class AdaptEngine:
    """θ(t) 在线学习：规则化梯度（信号表驱动），全部更新幅度可经 η_adapt 缩放。"""

    def __init__(
        self,
        eta_adapt: float = 0.6,
        threshold_floor: float = 1.2,
        threshold_max: float = 3.5,
        silence_hours: float = 2.0,
        silence_step: float = 0.10,
        reengage_minutes: float = 5.0,
        reengage_step: float = 0.05,
        streak_gate_step: float = 0.2,
        streak_recover: float = 0.10,
        suppress_hours: float = 6.0,
        q_gain: float = 0.10,
        eff_gain: float = 0.05,
        eff_drop: float = 0.10,
    ):
        self.eta_adapt = clamp(float(eta_adapt), 0.0, 1.0)
        self.threshold_floor = clamp(float(threshold_floor), 0.5, 3.0)
        self.threshold_max = clamp(float(threshold_max), 1.5, 6.0)
        self.silence_hours = clamp(float(silence_hours), 0.5, 24.0)
        self.silence_step = clamp(float(silence_step), 0.0, 1.0)
        self.reengage_minutes = clamp(float(reengage_minutes), 1.0, 120.0)
        self.reengage_step = clamp(float(reengage_step), 0.0, 0.5)
        self.streak_gate_step = clamp(float(streak_gate_step), 0.0, 1.0)
        self.streak_recover = clamp(float(streak_recover), 0.0, 1.0)
        self.suppress_hours = clamp(float(suppress_hours), 0.5, 48.0)
        self.q_gain = clamp(float(q_gain), 0.0, 0.5)
        self.eff_gain = clamp(float(eff_gain), 0.0, 0.3)
        self.eff_drop = clamp(float(eff_drop), 0.0, 0.5)

    # ---------- 用户消息 → 信号检测（§11 表） ----------

    def on_user_message(
        self, state: MindState, text: str, now: float,
        extra_positive: Optional[List[str]] = None,
        extra_negative: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """检测这条消息里的反馈信号，直接更新 θ/Q/g。返回事件列表（可解释性）。

        extra_positive/extra_negative：心理插件（shihai_psyche）扩展的主人口头禅词典。
        """
        events: List[Dict[str, Any]] = []
        text = (text or "").strip()
        if not text:
            return events
        low = text.lower().replace(" ", "")
        pos_words = list(_POS_WORDS) + [w for w in (extra_positive or []) if w]
        neg_words = list(_STRONG_NEG) + [w for w in (extra_negative or []) if w]
        strong_neg = any(w in text or w in low for w in neg_words)
        positive = (not strong_neg) and any(w in text or w in low
                                            for w in pos_words)
        short = (not positive and not strong_neg
                 and len(text) <= 3 and text in _SHORT_REPLIES)
        scale = self.eta_adapt if self.eta_adapt > 0 else 1.0
        # —— §5.8 r_feedback 连续化映射（EgoRegulator 的输入信号）：
        # 接续/正面评价 0.8-1.0；普通消息 0.6-0.8；短回复 0.2-0.4；强负反馈 0.0
        if strong_neg:
            state.r_feedback = 0.0
        elif short:
            state.r_feedback = clamp(state.r_feedback * 0.5 + 0.15, 0.0, 0.4)
        elif positive:
            state.r_feedback = clamp(state.r_feedback + 0.25, 0.6, 1.0)
        else:
            state.r_feedback = clamp(state.r_feedback + 0.05, 0.55, 0.8)
        # 冲突窗口置位（§5.10.2(b)：g_avoid≥0.6 时保持 3 个决策步，否则 1 步）
        if strong_neg or "生气" in text or "滚" in text:
            state.conflict_steps = 3 if state.goals.get("g_avoid", 0.0) >= 0.6 else 1
        # 正常交互清零连续未回复计数（§5.10.1 分段表的 streak）
        state.streak_count = 0

        # 1) 5 分钟内主动接续对话 → 正反馈：更主动（论文§11 第1行）
        if (state.last_bot_proactive_at > 0
                and now - state.last_bot_proactive_at <= self.reengage_minutes * 60
                and not state.last_proactive_replied):
            state.last_proactive_replied = True
            new_thr = max(self.threshold_floor,
                          state.theta["fire_threshold"] - self.reengage_step * scale)
            if new_thr < state.theta["fire_threshold"]:
                state.theta["fire_threshold"] = new_thr
                events.append({"type": "reengage", "effect": "fire_threshold↓",
                               "value": round(new_thr, 3)})
            self.q_update(state, state.last_proactive_action, True, scale)
            events.append({"type": "q_up", "action": state.last_proactive_action})

        # 2) 强负反馈："别老找我" → 压制 u 增速、抬高门槛、g_avoid↑（§11 第5行）
        if strong_neg:
            state.suppress_until = now + self.suppress_hours * 3600
            state.streak_gate = clamp(
                state.streak_gate + self.streak_gate_step * scale, 0.0,
                self.threshold_max - state.theta["fire_threshold"])
            state.urge = clamp(state.urge * 0.3, 0.0, 10.0)
            state.theta["eff"] = clamp(state.theta["eff"] - self.eff_drop, 0.0, 1.0)
            state.goals["g_avoid"] = clamp(state.goals["g_avoid"] + 0.15, 0.0, 1.0)
            self.q_update(state, state.last_proactive_action, False, scale)
            events.append({"type": "strong_negative",
                           "effect": f"suppress {self.suppress_hours}h, gate+"
                                     f"{self.streak_gate_step}, eff-{self.eff_drop}"})
            return events

        # 3) 正反馈："好/棒/谢谢" → Q↑、自我效能 +0.05、冷落门槛回落（§11 第2行）
        if positive:
            state.theta["eff"] = clamp(state.theta["eff"] + self.eff_gain, 0.0, 1.0)
            state.streak_gate = clamp(
                state.streak_gate - self.streak_recover * scale, 0.0, 6.0)
            action = state.last_proactive_action
            # 没有"真的发出去"的 action 时不计 Q（原来是 `or "chat_followup"` 兜底，
            # 把没执行的动作的账记到闲聊头上，Q 表越走越偏）
            if action:
                self.q_update(state, action, True, scale)
                events.append({"type": "positive",
                               "effect": f"eff+{self.eff_gain}, Q[{action}]↑, gate回落"})

        # 4) 短回复负反馈 → θ_tantrum↓ 收敛情绪（§11 第4行，floor 0.1）
        elif short:
            state.theta["tantrum_threshold"] = clamp(
                state.theta["tantrum_threshold"] - 0.05 * scale, 0.10, 2.0)
            self.q_update(state, "chat_followup", False, scale)
            events.append({"type": "short_reply",
                           "effect": f"tantrum_threshold↓ → "
                                     f"{round(state.theta['tantrum_threshold'], 3)}"})

        # 5) 恢复正常交互：冷落门槛继续回落（自洽报告 §9：阈值逐步回落）
        if not strong_neg:
            state.streak_gate = clamp(
                state.streak_gate - self.streak_recover * 0.5 * scale, 0.0, 6.0)
            # 兜底：只要主人开口了，挂起的主动消息就算有了回应，静默惩罚不再触发
            state.last_proactive_replied = True
        return events

    # ---------- 静默检测（§11 第3行） ----------

    def on_silence(self, state: MindState, now: float) -> List[Dict[str, Any]]:
        """主动消息发出后 >silence_hours 无人回 → FIRE_THRESHOLD↑（饱和上限内）。"""
        if (state.last_bot_proactive_at > 0
                and not state.last_proactive_replied
                and now - state.last_bot_proactive_at > self.silence_hours * 3600):
            state.last_proactive_replied = True  # 只惩罚一次
            new_thr = min(self.threshold_max,
                          state.theta["fire_threshold"] + self.silence_step)
            state.theta["fire_threshold"] = new_thr
            state.streak_gate = clamp(state.streak_gate + self.streak_gate_step,
                                      0.0, 6.0)
            state.streak_count += 1  # §5.10.1 分段表的连续未回复计数
            self.q_update(state, state.last_proactive_action, False, 1.0)
            return [{"type": "silence", "effect": f"fire_threshold→{round(new_thr, 3)}"
                                              f"（饱和上限 {self.threshold_max}）"}]
        return []

    # ---------- L_adapt：动作 Q 值（§5.8/§5.9） ----------

    def q_update(self, state: MindState, action: str, positive: bool,
                 scale: float = 1.0) -> None:
        if not action:
            return
        cur = state.q_table.get(action, 0.0)
        delta = self.q_gain * scale * (1.0 if positive else -1.0)
        state.q_table[action] = clamp(cur + delta, -1.0, 2.0)

    # ---------- 观测 ----------

    def threshold_effective(self, state: MindState) -> float:
        return min(state.theta["fire_threshold"] + state.streak_gate,
                   self.threshold_max)
