"""EgoRegulator 自我调节引擎（整合稿 §5.10 + §17 架构扩展）。

    r_ego = Ego(e, a, ψ, g, u, C, r_feedback)                        (5-10)
    发送条件：u > FIRE_THRESHOLD(有效) ∧ r_release > ρ               (5-10a)
    延迟：FIRE_THRESHOLD ← FIRE_THRESHOLD + δ·r_delay                (5-10b)

§5.10.1 r_release 分段规则（优先级自上而下，先命中先执行）：
    u ≥ URGE_CEILING          → 1.0   高位放行（真实表达意愿，宁多说一句不长期沉默）
    u < FIRE_THRESHOLD_LOW    → 0.0   低位拦截（气话/随口，连替代行为也不触发）
    conflict_flag == 1        → 抑制档
    r_feedback < 0.3          → 抑制档
    streak > 3                → 抑制档
    else                      → 1.0   默认放行
    抑制档连续值 = 0.30 + 0.20·min(1, r_feedback/0.30)，ψ_N≥0.6 时下限 0.35，
    恒 ≤ ρ=0.5（严格大于判定，落入抑制档必然不释放）。

§5.10.2 防御机制（优先级 displacement > repression > projection > rationalization）：
    displacement  抑制期内越阈 → 冲动导向替代行为（写日记/发动态/梦境固化），
                  不钳位、不清零 u——念头保留在累积路径上，等高位分支放行
    repression    streak≥3 ∨ r_feedback<0.2 → 记录 Δθ_ego（阈值↑召回偏置↓，接口保留）
    projection    意图推断置信度低 → 调整 s_ext 置信权重（接口保留）
    rationalization  反馈中性但念头徘徊门槛附近 → 叙事角度调整（接口保留）

§17.1 受控自我回流原语（reflux，默认关闭，与 §5.10 默认配置互斥，切换须注明）：
    p₁ 情绪调制  e ← e − ε·ê                     ε ≤ 0.15 分钟级
    p₂ 念头衰减  u ← u·(1−ε)                     ε ≤ 0.15 分钟级
    p₃ 能量转移  u ← u + ε·‖e‖₂; e ← e − ε·‖e‖₂·ê   ε ≤ 0.10 分钟级
    p₄ 图谱写入  反思节点（慢，小时级，由替代行为执行）
    看门狗强制：每步最多调用一个快原语（p₁-p₃ 互斥），调用全部写入 ego_trace
    可审计日志（时刻、原语、分量、增量），可复现可回放。

§17.2 稳定性：ρ(J_F)=0.294 + 两快原语扰动上界 0.30 = 0.594 < 1，余量 0.406。

§17.3 时间尺度分离：回流只作用于快变量 e/a/u；θ 与 ψ 的更新路径不接收回流。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .state import clamp, EMOTION_DIMS

RHO = 0.5            # 释放判定阈值（§5.10.1，严格大于）
EPS_E = 0.15         # p₁ 强度上限
EPS_U = 0.15         # p₂ 强度上限
EPS_TRANSFER = 0.10  # p₃ 强度上限


@dataclass
class EgoDecision:
    """一次 EgoRegulator 决策的完整输出（四路调节信号 + 审计信息）。"""
    r_release: float = 1.0
    r_delay: float = 0.0
    defense: str = "none"          # none/rationalization/repression/projection/displacement
    branch: str = ""               # 命中的分段规则名（可解释性）
    alternatives: List[str] = field(default_factory=list)  # displacement 的替代行为
    reflux: str = ""               # 本次调用的回流原语（"" = 未调用）
    reflux_delta: Dict[str, float] = field(default_factory=dict)
    trace: str = ""


class EgoRegulator:
    """确定性规则引擎，不调用 LLM，运行于 Core-Mind Runtime 内（§5.10.1）。"""

    def __init__(
        self,
        rho: float = RHO,
        low_ratio: float = 0.5,          # FIRE_THRESHOLD_LOW = low_ratio × FIRE_THRESHOLD
        reflux_enabled: bool = False,     # §17.1：只在扩展模式启用
        epsilon_max: float = 0.15,
        inhibit_floor: float = 0.30,
        inhibit_floor_high_n: float = 0.35,   # §5.10.1(a)：ψ_N≥0.6 时的抑制档下限
    ):
        self.rho = clamp(float(rho), 0.1, 0.9)
        self.low_ratio = clamp(float(low_ratio), 0.3, 0.8)
        self.reflux_enabled = bool(reflux_enabled)
        self.epsilon_max = clamp(float(epsilon_max), 0.05, 0.15)
        self.inhibit_floor = clamp(float(inhibit_floor), 0.0, 0.5)
        self.inhibit_floor_high_n = clamp(float(inhibit_floor_high_n), 0.0, 0.5)

    # ---------- §5.10.1 分段规则 ----------

    def release_permission(
        self,
        u: float,
        fire_threshold: float,
        urge_ceiling: float,
        conflict_flag: bool,
        r_feedback: float,
        streak: int,
        psi_N: float = 0.5,
        r_top1: float = 1.0,
        anchor_kind: str = "",
    ) -> float:
        """r_release ∈ [0,1]。规则表只输出端点，抑制档给连续值（§5.10.1 连续实现）。"""
        low = self.low_ratio * float(fire_threshold)
        if u >= float(urge_ceiling):
            return 1.0                        # ① 高位放行：真实表达意愿
        if u < low:
            return 0.0                        # ② 低位拦截：气话/随口
        # §5.10.2(c) a 通道：top-1 相关度低时，事由回访类强制抑制档
        revisit_like = anchor_kind in ("revisit", "promise")
        floor = self.inhibit_floor_high_n if float(psi_N) >= 0.6 else self.inhibit_floor
        inhibit = max(floor, self.inhibit_floor + 0.20 * min(1.0, max(0.0, float(r_feedback)) / 0.30))
        inhibit = min(inhibit, self.rho)      # 恒不高于 ρ（判据为严格大于）
        if conflict_flag:
            return inhibit                    # ③ 冲突窗口
        if float(r_feedback) < 0.3:
            return inhibit                    # ④ 强负反馈
        if int(streak) > 3:
            return inhibit                    # ⑤ 连续冷落
        if revisit_like and float(r_top1) < 0.4:
            return inhibit                    # ⑥(c) a 通道：锚点相关度不足
        return 1.0                            # ⑦ 默认放行

    # ---------- §5.10 决策主入口 ----------

    def decide(
        self,
        u: float,
        fire_threshold: float,
        urge_ceiling: float,
        e: Dict[str, float],
        psi: Dict[str, float],
        goals: Dict[str, float],
        conflict_flag: bool,
        streak: int,
        r_feedback: float,
        r_top1: float = 1.0,
        anchor_kind: str = "",
        over_threshold: bool = False,
    ) -> EgoDecision:
        """完整决策：分段许可 + r_delay + 防御选择（§5.10.1 + §5.10.2）。"""
        d = EgoDecision()
        d.r_release = self.release_permission(
            u, fire_threshold, urge_ceiling, conflict_flag,
            r_feedback, streak, float(psi.get("N", 0.5)), r_top1, anchor_kind)
        # r_delay（§5.10.1 末）：冲突后冷却期或连续两轮以上未获回复 → 0.15
        d.r_delay = 0.15 if (conflict_flag or int(streak) >= 2) else 0.0
        # 防御机制（§5.10.2，优先级 displacement > repression > projection > rationalization）
        low = self.low_ratio * float(fire_threshold)
        if u >= low and d.r_release <= self.rho:
            d.defense = "displacement"
            d.alternatives = ["reflect_diary", "dream_consolidate"]  # 内部替代行为（表 17-3：不对外输出）
            d.branch = "越阈但自我未许可 → 冲动转入替代行为"
        elif int(streak) >= 3 or float(r_feedback) < 0.2:
            d.defense = "repression"
            d.branch = "连续冷落/强负反馈 → 压抑（阈值↑召回偏置↓）"
        elif float(r_top1) < 0.5:
            d.defense = "projection"
            d.branch = "意图推断置信度低 → 下调外部刺激解释权重"
        elif (not conflict_flag and 0.3 <= float(r_feedback) < 0.6
              and abs(u - float(fire_threshold)) < 0.5 and u >= low):
            d.defense = "rationalization"
            d.branch = "反馈中性且念头徘徊门槛附近 → 文饰"
        # 高位/低位分支标注
        if d.branch == "":
            if u >= float(urge_ceiling):
                d.branch = "高位放行（u 顶格，视为真实表达意愿）"
            elif u < low:
                d.branch = "低位拦截（气话/随口，不触发任何输出）"
            else:
                d.branch = "默认放行"
        d.trace = (f"r_release={d.r_release:.2f} r_delay={d.r_delay:.2f} "
                   f"defense={d.defense} branch={d.branch}")
        return d

    # ---------- §17.1 受控回流原语 ----------

    @staticmethod
    def _unit_e(e: Dict[str, float]) -> Dict[str, float]:
        """ê：情绪向量的单位方向（L2 归一化）。"""
        norm = (sum(v * v for v in e.values()) ** 0.5) or 1.0
        return {k: float(v) / norm for k, v in e.items()}

    def reflux_p1_calm(self, e: Dict[str, float], eps: float) -> tuple:
        """p₁ 情绪调制（快）：把残余情绪压回基线。e ← e − ε·ê。返回(新e, 增量范数)。"""
        eps = min(float(eps), EPS_E)
        hat = self._unit_e(e)
        new_e = {k: clamp(float(v) - eps * hat[k], 0.0, 1.0) for k, v in e.items()}
        delta = eps  # ‖ε·ê‖ = ε
        return new_e, delta

    def reflux_p2_dissolve(self, u: float, eps: float) -> tuple:
        """p₂ 念头衰减（快）：反复被抑制时消解冲动。u ← u − ε·min(u, u_ref)，
        u_ref 取 u 本身（u ≥ 低位时 min = u，等价 u ← u·(1−ε)；§17.2 实测悬停 2.1）。"""
        eps = min(float(eps), EPS_U)
        u_ref = max(0.0, float(u))
        new_u = clamp(float(u) - eps * min(u, u_ref), 0.0, 10.0)
        return new_u, abs(new_u - float(u))

    def reflux_p3_transfer(self, e: Dict[str, float], u: float, eps: float) -> tuple:
        """p₃ 能量转移（快）：情绪转为"想说"的动力。
        u ← u + ε·‖e‖₂；e ← e − ε·‖e‖₂·ê。"""
        eps = min(float(eps), EPS_TRANSFER)
        norm = (sum(v * v for v in e.values()) ** 0.5)
        hat = self._unit_e(e)
        new_u = clamp(float(u) + eps * norm, 0.0, 10.0)
        new_e = {k: clamp(float(v) - eps * norm * hat[k], 0.0, 1.0)
                 for k, v in e.items()}
        return new_e, new_u, eps * norm

    def pick_reflux(self, u: float, e: Dict[str, float],
                    fire_threshold: float) -> tuple:
        """看门狗选择：每步最多一个快原语（§17.2 强制）。策略：
        越阈(u>阈值) → p₂ 消解冲动；‖e‖₂ 大 → p₁ 压情绪；否则 p₃ 转移。
        reflux_enabled=False 时返回 ("", ...) 表示不调用。"""
        if not self.reflux_enabled:
            return "", e, u, 0.0
        norm_e = (sum(v * v for v in e.values()) ** 0.5)
        if u > float(fire_threshold):
            new_u, delta = self.reflux_p2_dissolve(u, self.epsilon_max)
            return "p2", e, new_u, delta
        if norm_e > 0.25:
            new_e, delta = self.reflux_p1_calm(e, self.epsilon_max)
            return "p1", new_e, u, delta
        new_e, new_u, delta = self.reflux_p3_transfer(e, u, self.epsilon_max)
        return "p3", new_e, new_u, delta

    # ---------- §17.1(iii) 可审计轨迹 ----------

    @staticmethod
    def trace_entry(ts: float, primitive: str, target: str,
                    delta: float) -> Dict[str, Any]:
        return {"ts": round(float(ts), 3), "primitive": primitive,
                "target": target, "delta": round(float(delta), 4)}

    def audit(self, entries: List[Dict[str, Any]], keep: int = 200) -> List[Dict[str, Any]]:
        """ego_trace 环形保留最近 keep 条。"""
        return entries[-keep:]
