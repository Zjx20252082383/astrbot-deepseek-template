"""情绪场动力学（论文 §5.1 情绪场方程、§5.4 情绪-衰减耦合、§6 弛豫常数、§8 人格调制）。

    τ_i · de_i/dt = -e_i + κ_i·[s_ext,i + s_int,i] + η·Σ_j W_ij·e_j

- τ_i  : 弛豫时间常数（小时）。love=24h 最长 → 好感消退最慢，是"陪伴感"的数学基础。
- -e_i : 自然衰减项，无刺激时情绪向 0 回归。
- s_ext: 外部情绪刺激（用户输入推断，classify.estimate_emotion）。
- s_int: 内部情绪刺激（记忆召回注入，graph.recall_emotion_injection）。
- η·W·e: 情绪互激励（love 与 happy 正相关、fear 与 angry 正相关）→ 情绪惯性。

数值积分：显式欧拉，tick 粒度 dt 小时，结果钳位 [0,1]。
自洽性检查报告（第4页 §5）：±20% 摄动无发散 —— 参数不敏感，欧拉步长足够。
"""

from __future__ import annotations

from typing import Dict, Optional

from .state import EMOTION_DIMS, clamp

# 论文 §6 表：各情绪分量弛豫时间常数（小时）。surprise 论文未给，取 1h（最易消退）。
DEFAULT_TAU: Dict[str, float] = {
    "happy": 6.0, "sad": 12.0, "angry": 2.0, "fear": 3.0, "love": 24.0,
    "surprise": 1.0,
}

# 6×6 情绪互激励矩阵 W（行=受激情绪，列=来源情绪）。论文 §5.1：
# love↔happy 正相关、fear↔angry 正相关，另补 sad↔fear、surprise→happy 轻度联动。
DEFAULT_W: Dict[str, Dict[str, float]] = {
    "happy":    {"love": 0.15, "surprise": 0.10},
    "sad":      {"fear": 0.08},
    "angry":    {"fear": 0.10},
    "fear":     {"angry": 0.12, "sad": 0.08},
    "love":     {"happy": 0.12},
    "surprise": {"happy": 0.08},
}

# 论文 §7.2 表：节点类型基础衰减率（每小时）。highlight/创伤 0.005 → 长期难忘。
LAMBDA_TYPE: Dict[str, float] = {
    "fact": 0.10, "emotion": 0.01, "event": 0.05, "highlight": 0.005,
}


class EmotionField:
    """e(t) 的 ODE 积分器 + λ_v(e) 耦合项。ψ（大五）通过构造时传入的引用调制参数。"""

    def __init__(
        self,
        tau: Optional[Dict[str, float]] = None,
        kappa: float = 0.8,
        eta: float = 0.15,
        w_matrix: Optional[Dict[str, Dict[str, float]]] = None,
        psi: Optional[Dict[str, float]] = None,
    ):
        self.tau = dict(DEFAULT_TAU)
        if tau:
            for k, v in tau.items():
                if k in self.tau:
                    self.tau[k] = max(0.1, float(v))
        self.kappa = clamp(float(kappa), 0.0, 3.0)
        self.eta = clamp(float(eta), 0.0, 1.0)
        self.w = {d: dict(DEFAULT_W.get(d, {})) for d in EMOTION_DIMS}
        if w_matrix:
            for row, cols in w_matrix.items():
                if row in self.w and isinstance(cols, dict):
                    for col, val in cols.items():
                        if col in EMOTION_DIMS:
                            self.w[row][col] = clamp(float(val), 0.0, 1.0)
        self.psi = psi if psi is not None else {}

    # ---------- §8 人格调制 ----------

    def tau_eff(self, dim: str) -> float:
        """有效弛豫时间。论文 §8：τ_fear = τ₀·(1+N)，神经质越高恐惧消退越慢。"""
        t = self.tau.get(dim, 6.0)
        if dim == "fear":
            t *= 1.0 + clamp(float(self.psi.get("N", 0.35)), 0.0, 1.0)
        return max(0.1, t)

    # ---------- §5.1 情绪场方程 ----------

    def step(
        self,
        e: Dict[str, float],
        s_ext: Dict[str, float],
        s_int: Dict[str, float],
        dt_hours: float,
    ) -> Dict[str, float]:
        """推进一个 dt。s_ext/s_int 缺省维度按 0 处理。"""
        if dt_hours <= 0:
            return dict(e)
        e_next: Dict[str, float] = {}
        for dim in EMOTION_DIMS:
            drive = self.kappa * (
                float(s_ext.get(dim, 0.0)) + float(s_int.get(dim, 0.0))
            )
            cross = self.eta * sum(
                w * float(e.get(src, 0.0))
                for src, w in self.w.get(dim, {}).items()
            )
            de = dt_hours / self.tau_eff(dim) * (-float(e.get(dim, 0.0)) + drive + cross)
            e_next[dim] = clamp(float(e.get(dim, 0.0)) + de, 0.0, 1.0)
        return e_next

    # ---------- §5.4 情绪-衰减耦合项 ----------

    @staticmethod
    def lambda_eff(lambda_type: float, e_dom: float, e_node: float, alpha: float) -> float:
        """λ_v(e) = λ_type / (1 + α·e_dom·E_v)。

        开心时美好记忆衰减更慢（λ 变小 = 保留更久），情绪对记忆的反向调制。
        alpha：情绪-衰减耦合强度，论文取 3.0。
        """
        e_node = clamp(float(e_node), 0.0, 1.0)
        e_dom = clamp(float(e_dom), 0.0, 1.0)
        denom = 1.0 + max(0.0, float(alpha)) * e_dom * e_node
        return float(lambda_type) / max(0.05, denom)

    # ---------- 观测 ----------

    @staticmethod
    def dominant(e: Dict[str, float]) -> tuple[str, float]:
        dim = max(e, key=lambda k: e.get(k, 0.0))
        return dim, float(e.get(dim, 0.0))
