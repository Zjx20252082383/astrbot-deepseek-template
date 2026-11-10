"""念头引擎（论文 §5.6 念头方程、§9 念头参数与由头锚点）。

    du/dt = interest(e, a) × timing_fit(t) × mood_state(e)

物理含义（§5.6）：念头不是定时器，是一个可积累的量——越在意的人攒得越快，
刚聊过清零，被冷落门槛自动抬高（STREAK_GATE_STEP）。

参数（§9 表，全部可在 _conf_schema.json 调）：
    FIRE_THRESHOLD  2.10        念头攒到此值才主动开口
    URGE_CEILING    3.0         念头上限
    STREAK_GATE_STEP 0.2        被冷落一次门槛 +0.2
    RHYTHM_ON/OFF   1.45 / 0.48 作息命中/不命中时段的增速

自洽性检查报告 §9 补充：自适应阈值带硬饱和上限 3.5，防止极端负反馈下永久沉默。
"""

from __future__ import annotations

from typing import Dict, Optional

from .state import clamp


class UrgeEngine:
    """u(t) 积分器。调用方每 tick 调一次 step()，隔天用 should_fire() 判断开口。"""

    def __init__(
        self,
        base_rate: float = 0.55,
        rhythm_on: float = 1.45,
        rhythm_off: float = 0.48,
        quiet_start: int = 1,
        quiet_end: int = 8,
        urge_ceiling: float = 3.0,
        active_hours: tuple[int, int] = (8, 24),
        psi_ref: Optional[Dict[str, float]] = None,
    ):
        self.base_rate = clamp(float(base_rate), 0.0, 3.0)
        self.rhythm_on = clamp(float(rhythm_on), 0.0, 3.0)
        self.rhythm_off = clamp(float(rhythm_off), 0.0, 3.0)
        self.quiet_start = int(clamp(float(quiet_start), 0, 23))
        self.quiet_end = int(clamp(float(quiet_end), 0, 23))
        self.urge_ceiling = clamp(float(urge_ceiling), 1.0, 10.0)
        self.active_hours = active_hours
        self.psi_ref = psi_ref if psi_ref is not None else {}

    # ---------- §5.6 三个因子 ----------

    def interest(self, e: Dict[str, float], recent_max_a: float) -> float:
        """兴趣度 [0,1]：当前情绪（love/happy）与激活记忆共同决定。"""
        warm = 0.5 * (float(e.get("love", 0.0)) + float(e.get("happy", 0.0)))
        return clamp(0.5 * warm + 0.5 * clamp(recent_max_a, 0.0, 1.0), 0.0, 1.0)

    def timing_fit(self, hour: int) -> float:
        """时间适配度：安静时段 0（绝不打扰），作息命中 RHYTHM_ON，其余 RHYTHM_OFF。"""
        h = int(hour) % 24
        if self.quiet_start <= h < self.quiet_end or (
            self.quiet_start > self.quiet_end
            and (h >= self.quiet_start or h < self.quiet_end)
        ):
            return 0.0
        if self.active_hours[0] <= h < self.active_hours[1]:
            return self.rhythm_on
        return self.rhythm_off

    def mood_state(self, e: Dict[str, float]) -> float:
        """情绪状态因子：低落时念头发散慢，积极时发散快。钳位 [0.2, 1.5]。"""
        positive = float(e.get("love", 0.0)) + float(e.get("happy", 0.0))
        negative = (float(e.get("sad", 0.0)) + float(e.get("angry", 0.0))
                    + float(e.get("fear", 0.0)))
        return clamp(1.0 + 0.5 * positive - 0.6 * negative, 0.2, 1.5)

    # ---------- 积分 ----------

    def step(
        self,
        u: float,
        e: Dict[str, float],
        recent_max_a: float,
        hour: int,
        dt_hours: float,
        suppressed: bool = False,
    ) -> float:
        """推进 du/dt 一个 tick。suppressed=True（"别老找我"生效期）时 u 只降不涨。"""
        if suppressed:
            return clamp(u - 0.1 * dt_hours, 0.0, self.urge_ceiling)
        fit = self.timing_fit(hour)
        if fit <= 0.0:
            return u  # 安静时段冻结（不清零，醒来接着攒）
        # §8：p_initiate ∝ E（外向性越高越主动找人说话）
        e_scale = clamp(float(self.psi_ref.get("E", 0.6)), 0.0, 1.0)
        base = self.base_rate * (0.5 + 0.5 * e_scale)
        du = base * self.interest(e, recent_max_a) * fit * self.mood_state(e) * dt_hours
        return clamp(u + du, 0.0, self.urge_ceiling)

    def should_fire(self, u: float, fire_threshold: float, streak_gate: float,
                    threshold_max: float = 3.5) -> bool:
        """u ≥ 有效阈值即开口。有效阈值 = 基准 + 冷落累计，硬饱和 threshold_max。"""
        eff = min(float(fire_threshold) + float(streak_gate), float(threshold_max))
        return u >= eff

    # ---------- §9 由头锚点 ----------

    ANCHOR_HINTS = {
        "revisit": "想起一件以前的事（回访旧事），自然地跟用户提起，像顺手想起一样",
        "promise": "之前聊过一件没下文的事（承诺跟进），关心一句后来怎么样了",
        "recent": "有一阵没跟用户说话了（关心近况），自然地打个招呼问一句在忙什么",
    }

    @staticmethod
    def anchor_hint(kind: str) -> str:
        return UrgeEngine.ANCHOR_HINTS.get(kind, "找个自然的理由说句话")
