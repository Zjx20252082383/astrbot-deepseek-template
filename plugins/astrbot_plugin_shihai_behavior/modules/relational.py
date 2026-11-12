"""关系状态 z_r(o) 与共享层可见性门控（论文 6.10 / 6.11.1 的落地实现）。

公式与稿件逐式对应：

    (6-18')  dR/dt = Π_R{ α[ρ σ(κ v) − R] − β(R − R_min) + γ c },  R ∈ [R_min, 1]
    (6-19)   ρ = n/(n + n0),   σ(x) = 1/(1 + e^{-x})
    (6-20)   R* = (α ρ σ + β R_min + γ c) / (α + β)
    (6-21)   w = 1（owner 会话） / σ(κ_R (R_o − R_gate))（其他会话）

参数（6.10.2 / 6.10.4 / 6.11.1）：
    α = 0.25 h⁻¹, β = −ln(DECAY)/Δt，DECAY = 0.94，Δt = 0.5 h → β = 0.1238 ≈ 0.12
    R_min = 0.15, n0 = 5, κ = 2.0, γ = 0.10
    R_gate = 0.40, κ_R = 6.0, Δt = 0.5 h（与引擎 tick 同频，|Δt(α+β)| = 0.185 < 2）
    n 的滑动窗 = 24 h（与 Hebbian 的 near_window 同窗，避免两套时间口径）

【评审记录】稿件 6.10.2 写"β 直接复用既有 decay_factor"、并给出 0.94。实测
memory_v2 里三个 decay_factor 是 0.992 / 0.996 / 0.998，全库只赋值不读取；
−ln(0.992)/0.5 = 0.016，与 0.12 差近一个量级。因此本模块**不**声称复用记忆层
字段：0.94 在这里显式声明为**关系层专用常数**（REL_DECAY），β 由它按同一公式
反推。数值口径仍取稿件 6.10.3–6.10.4 的 0.12，否则 6-20 的稳态 0.32 与
ν = 1/β ≈ 8.1 h 全部不成立。

投影语义（6.10.3）：Π_R 写在向量场里，不是"积分完再钳位"。离散实现里体现为
"先取 Π 允许的增量、再代入"，与"越界后 clamp"在边界点等价，但语义是动力学
定义——下界因此是吸引子（f < 0 的分量被投影吃掉），不会出现越界后弹回。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 参数（论文 6.10.2 / 6.10.4 / 6.11.1）
# --------------------------------------------------------------------------
ALPHA = 0.25            # 关系建立速率 h⁻¹
REL_DECAY = 0.94        # 关系层专用衰减因子（**不是** memory_v2 的 0.992/0.996/0.998）
DT_HOURS = 0.5          # 积分步长（引擎 tick 30 min）
BETA = -math.log(REL_DECAY) / DT_HOURS   # ≈ 0.1238 h⁻¹
R_MIN = 0.15            # 关系下界（"曾经认识过"的最低残留）
N0 = 5.0                # 互动率半饱和常数（5 次互动达到半饱和）
KAPPA = 2.0             # 效价压缩增益
GAMMA = 0.10            # 未完成承诺项增益（与 ego EPS 第三项同值）
R_GATE = 0.40           # 门控阈值
KAPPA_R = 6.0           # 门控陡度
WINDOW_SEC = 24 * 3600  # 互动率滑动窗（与 Hebbian near_window 一致）

# 冷启动默认值（6.12.5：陌生号首次说话 R_o 取默认值，必须能报出来当协变量）
# 分三档，避免把"第一次出现但其实是熟人"按陌生人处理（0.15→w=0.18→名额 2）：
#   owner 会话 1.0（硬校验；门控本身恒等 1，这个值只进关系层记录与亲疏措辞）
#   已知熟人  0.70（事实库里出现过该 owner 的对象，w=0.858 → 名额 ≈⌈0.86K⌉）
#   其余     0.15（真陌生，w=0.182）
R_DEFAULT = R_MIN
R_DEFAULT_OWNER = 1.0
R_DEFAULT_KNOWN = 0.70


def sigmoid(x: float) -> float:
    """σ(x) = 1/(1+e^{-x})；大负数不溢出。"""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def project(r: float) -> float:
    """Π_R：把状态投影回 [R_min, 1]。"""
    return max(R_MIN, min(1.0, float(r)))


def rho(n: float) -> float:
    """互动率 ρ = n/(n+n0)。"""
    n = max(0.0, float(n))
    return n / (n + N0)


def visible_weight(r_o: Optional[float], is_owner: bool) -> float:
    """式 (6-21)：owner 分支恒等 1，其他会话按 σ(κ_R(R_o − R_gate))。

    owner 必须恒等 1：门控要回答的是"他人能看到多少主人的共享层"，对主人自己
    做门控等于限制他看自己的记忆；若纳入 σ(·)，冷启动时 R_owner 可能低于门控
    阈值，M2 会被自己设计的门控压低，结论无法归因（6.11.1）。
    """
    if is_owner:
        return 1.0
    if r_o is None:
        return 0.0
    return sigmoid(KAPPA_R * (float(r_o) - R_GATE))


def quota(w: float, k: int) -> int:
    """共享层名额 ⌈w·K_shared⌉（6.11.1：参与预算分配而非硬性剔除）。"""
    if k <= 0:
        return 0
    if w <= 0.0:
        return 0
    return max(0, min(int(k), int(math.ceil(w * k - 1e-9))))


def step_once(r: float, rho_v: float, valence: float, commitment: int,
              dt: float = DT_HOURS) -> float:
    """一步投影积分：R_{k+1} = Π_R{R_k + Δt·f(R_k)}。"""
    r = project(r)
    target = rho_v * sigmoid(KAPPA * float(valence))
    f = ALPHA * (target - r) - BETA * (r - R_MIN) + GAMMA * (1.0 if commitment else 0.0)
    nxt = r + dt * f
    # 边界处理即法锥条件：下界处只接受 dR/dt ≥ 0，上界处只接受 ≤ 0
    if r <= R_MIN + 1e-12 and nxt < R_MIN:
        return R_MIN
    if r >= 1.0 - 1e-12 and nxt > 1.0:
        return 1.0
    return project(nxt)


def steady_state(rho_v: float, valence: float, commitment: int) -> float:
    """式 (6-20) 的稳态，再过一次 Π_R。

    注意：裸公式在 c=0、ρ=0 时给 (β R_min)/(α+β) = 0.0187/0.3738 ≈ 0.05，低于
    R_min；但投影写在向量场里，下界处 f<0 的分量被 Π 吃掉，实际状态停在下界
    0.15。这正是 6.10.3 要的"不归零、留残留"——报告里给的稳态值必须是投影后的。
    """
    target = rho_v * sigmoid(KAPPA * float(valence))
    raw = (ALPHA * target + BETA * R_MIN + GAMMA * (1.0 if commitment else 0.0)) \
        / (ALPHA + BETA)
    return project(raw)


class RelationStore:
    """按对象持久的关系状态；每个对象一条 R、24h 互动窗、效价窗、承诺位。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.items: Dict[str, Dict[str, Any]] = {}
        self.last_tick: float = 0.0
        self._last_save: float = 0.0
        # 已知熟人集合（从事实库 owner 字段汇总，启动时由 memory.py 注入）。
        # 只影响"缺行时给什么初值"，不改变已有行的 R。
        self.known: set[str] = set()
        self._load()

    def pin_owner(self) -> bool:
        """把 owner 行的 R 钉到 R_DEFAULT_OWNER（老库是 0.55 的旧默认值）。

        owner 的门控本来就恒等 1（式 6-21），这个 R 只进关系层记录与亲疏措辞；
        钉值是为了让"主人=最熟"在状态文件里也自洽，不参与任何门控计算。
        已经推进过（有 wins）的 owner 行不动，避免改写历史轨迹。
        """
        row = self.items.get("owner")
        if not row:
            self._row("owner")
            return True
        # 不看 wins：owner 的 R 不参与门控（式 6-21 恒等 1），旧库里的 0.55 只是
        # 早期默认值残留，钉成 1.0 才不会在 state 文件里和常量自相矛盾。
        try:
            if abs(float(row.get("R", 0.0)) - R_DEFAULT_OWNER) < 1e-6:
                return False
        except (TypeError, ValueError):
            pass
        row["R"] = R_DEFAULT_OWNER
        return True

    def register_known(self, objs) -> int:
        """登记已知熟人；返回本次被抬高初值的对象数。

        判据是"事实库里出现过这个 owner"，而不是"聊过天"：有记忆 = 认识。
        只在缺行、或行还是纯默认值（R==R_DEFAULT 且 24h 窗为空）时抬到
        R_DEFAULT_KNOWN，绝不覆盖已经被动力学推进过的真实状态。
        """
        seeded = 0
        for raw in (objs or ()):
            obj = str(raw or "").strip()
            if not obj or obj == "owner":
                continue
            self.known.add(obj)
            row = self.items.get(obj)
            if row is None:
                self._row(obj)          # 走 _default_r，天然是熟人档
                seeded += 1
                continue
            try:
                quiet = not (row.get("wins") or [])
                if quiet and abs(float(row.get("R", 0.0)) - R_DEFAULT) < 1e-6:
                    row["R"] = R_DEFAULT_KNOWN
                    seeded += 1
            except (TypeError, ValueError):
                continue
        return seeded

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 损坏时按空库起，不阻断启动
            return
        rows = data.get("items") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            self.items = {str(k): v for k, v in rows.items() if isinstance(v, dict)}
        try:
            self.last_tick = float(data.get("last_tick", 0.0) or 0.0)
        except (TypeError, ValueError):
            self.last_tick = 0.0

    def save(self) -> None:
        data = {"items": self.items, "last_tick": self.last_tick,
                "params": {"alpha": ALPHA, "beta": round(BETA, 4),
                           "r_min": R_MIN, "n0": N0, "kappa": KAPPA,
                           "gamma": GAMMA, "r_gate": R_GATE, "kappa_r": KAPPA_R}}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, str(self.path))
            self._last_save = time.time()
        except OSError:
            pass

    # ---------- 内部 ----------
    def _default_r(self, key: str) -> float:
        if key == "owner":
            return R_DEFAULT_OWNER
        if key in self.known:
            return R_DEFAULT_KNOWN
        return R_DEFAULT

    def _row(self, obj: str) -> Dict[str, Any]:
        key = str(obj or "owner")
        row = self.items.get(key)
        if row is None:
            row = {
                "R": self._default_r(key),
                "wins": [],          # [[ts, valence], ...] 24h 窗
                "c": 0,              # 未完成承诺
                "last_ts": 0.0,      # 上次积分时刻
                "notes": 0,          # 累计互动条数（审计用）
            }
            self.items[key] = row
        return row

    def _trim(self, row: Dict[str, Any], now: float) -> None:
        lo = now - WINDOW_SEC
        wins: List[List[float]] = row.get("wins") or []
        row["wins"] = [w for w in wins if float(w[0]) >= lo][-200:]

    def _advance(self, obj: str, now: Optional[float] = None) -> float:
        """把该对象积分到 now（每 0.5 h 一步，最多回补 400 步=200 h）。"""
        now = float(now or time.time())
        row = self._row(obj)
        last = float(row.get("last_ts") or 0.0)
        if last <= 0:
            row["last_ts"] = now
            return float(row.get("R", R_DEFAULT))
        dt = now - last
        if dt < DT_HOURS * 3600:
            return float(row.get("R", R_DEFAULT))
        steps = min(400, int(dt // (DT_HOURS * 3600)))
        r = float(row.get("R", R_DEFAULT))
        # 承诺位先落到窗口起点前，保证逐步积分用的是当时的值
        c = 1 if int(row.get("c") or 0) else 0
        for i in range(steps):
            t = last + (i + 1) * DT_HOURS * 3600
            self._trim(row, t)
            n = len(row.get("wins") or [])
            vals = [float(w[1]) for w in (row.get("wins") or [])]
            v = (sum(vals) / len(vals)) if vals else 0.0
            r = step_once(r, rho(n), v, c)
        row["R"] = round(r, 4)
        row["last_ts"] = now
        self.last_tick = now
        return float(row["R"])

    # ---------- 对外 ----------
    def note(self, obj: str, valence: float, ts: Optional[float] = None) -> float:
        """记一次互动：入 24h 窗，顺便把状态积分到此刻。"""
        ts = float(ts or time.time())
        key = str(obj or "owner")
        self._advance(key, ts - DT_HOURS * 3600 * 0.5)  # 用上一刻的状态做积分终点
        row = self._row(key)
        wins: List[List[float]] = row.get("wins") or []
        wins.append([ts, round(max(-1.0, min(1.0, float(valence))), 3)])
        row["wins"] = wins[-200:]
        row["notes"] = int(row.get("notes") or 0) + 1
        r = self._advance(key, ts)
        # 看门狗 30 min 才 step_all 落盘，这里按 5 min 节流补一次，避免重启丢窗
        if ts - float(self._last_save or 0) > 300:
            self.save()
        return r

    def set_commitment(self, obj: str, flag: bool, ts: Optional[float] = None) -> None:
        row = self._row(str(obj or "owner"))
        row["c"] = 1 if flag else 0

    def get(self, obj: str, now: Optional[float] = None) -> float:
        return self._advance(str(obj or "owner"), now)

    def step_all(self, now: Optional[float] = None) -> None:
        """看门狗每轮调用：所有对象推进到 now；真推进了才落盘一次。

        看门狗 20 s 一跳，而积分步长是 0.5 h：大多数轮次什么都不会变，无脑
        save 就是每 20 s 重写一遍 JSON。
        """
        now = float(now or time.time())
        before = {k: v.get("R") for k, v in self.items.items()}
        for obj in list(self.items.keys()):
            self._advance(obj, now)
        if any(self.items[k].get("R") != before.get(k) for k in self.items):
            self.save()

    def snapshot(self) -> Dict[str, Any]:
        return {k: {"R": round(float(v.get("R", 0.0)), 3),
                    "n24h": len(v.get("wins") or []),
                    "c": int(v.get("c") or 0)}
                for k, v in self.items.items()}


def valence_from_text(text: str) -> float:
    """词典代理效价 v ∈ [-1,1]。

    **工程先验**，只用作 z_r/z_s 的输入信号，不参与记忆内容判定、不进注入文案。
    稿件 6.10.2 只要求 v_o 是"累积情绪效价"，没有规定怎么算；本实现选最便宜的
    可复现口径：正负词命中数之差 / (命中总数 + 1)，避免长句被一次命中拉满。
    """
    t = str(text or "")
    pos = sum(t.count(w) for w in _POS_WORDS)
    neg = sum(t.count(w) for w in _NEG_WORDS)
    if pos + neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / float(pos + neg + 1)))


_POS_WORDS = (
    "开心", "高兴", "喜欢", "爱你", "谢谢", "厉害", "棒", "哈哈", "嘿嘿",
    "好耶", "抱抱", "乖", "顺利", "成功", "过了", "考好", "赢了", "舒服", "可爱",
)
_NEG_WORDS = (
    "难过", "烦", "讨厌", "生气", "滚", "哭", "累", "疼", "病", "失败",
    "挂了", "崩溃", "吵架", "凶", "失望", "无聊", "不想", "怕", "焦虑", "委屈",
)
