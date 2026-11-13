"""关系子系统：多主体关系强度的可计算表示。

论文 §10.6。补的是 g(t) 里 g_relation 之外缺的那一半：

    g_relation  是"此刻想不想维持关系"的动机标量，端级、随互动推拉
    本模块       是"这段关系有多强"的度量，月级、按人分开

两个可观测输入，取自社会渗透理论（Altman & Taylor 1973）的广度/深度分解：

    广度 breadth = 与该人讨论过的话题集合大小
    深度 depth   = 该人自述的私密事实条数

两者都是计数，不引入不可观测的心理量。关系强度分两步走：

    向目标收敛  R ← R + k_A·(R_target - R)·STEP     （每次有效接触）
    时间衰减    R ← R·0.5^(Δt/HALF_LIFE_H)          （30 天半衰期）

k_A = 0.5 + 0.5·A，与念头累积的 (0.5+0.5·E) 同族：宜人性调制"一次互动能把
关系推动多远"。A∈[0,1] 时 k_A∈[0.5,1.0]，白拿一半，不会让低宜人性的人
完全推不动关系——这是有意的下界，不是 bug。

一条必须说清的限制：depth 的判据是第一人称自述 + 私密词表命中，属启发式
（见 _is_self_disclosure）。误判率未标定，本模块不声称它是可靠的自述检测器。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Set

# —— 参数（全部集中在这里，便于论文与代码对齐）——
BREADTH_SAT = 8.0      # 话题数到 8 个时广度饱和（1-exp(-1)=0.63，到 16 约 0.86）
DEPTH_SAT = 5.0        # 私密自述到 5 条时深度饱和
SURFACE_FLOOR = 0.4    # 只聊过表面话题时的关系上限：广度满分也只到 0.4
STEP = 0.5             # 每次接触向目标推进一步的比例
HALF_LIFE_H = 30 * 24.0  # 关系基线半衰期 30 天（与 ψ 同为月级慢变量）
K_A_BASE = 0.5         # k_A = K_A_BASE + (1-K_A_BASE)·A
MAX_PERSONS = 64       # 边数上限，超出按 R 最小淘汰（与图谱 _cap_size 同思路）

# 私密自述判据用的词表：命中任一项才算"讲了关于自己的事"
_SELF_MARKS = ("我", "俺", "咱")
_PRIVATE_WORDS = (
    "妈", "爸", "家", "爸", "爸", "工资", "钱紧", "借钱", "病", "医院", "药",
    "疼", "失眠", "睡不着", "分手", "吵", "喜欢", "暗恋", "害怕", "焦虑",
    "抑郁", "累", "孤独", "想哭", "哭", "秘密", "不敢说", "别告诉",
    "担心", "后悔", "丢人", "自卑", "被", "考砸", "挂了", "退学", "辞职",
)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _is_self_disclosure(text: str) -> bool:
    """启发式：第一人称出现 + 私密词表命中，视为一条关于自己的自述。

    为什么用启发式而不是分类器：本模块要能在 tick 里同步跑完，不调模型。
    代价是误判——"我妈也说这电影好看"会被算成一次自述。误判率未标定，
    论文里按"启发式、未校准"标注，不按"识别的准确率"标注。
    """
    t = text or ""
    if len(t) < 4:
        return False
    if not any(m in t for m in _SELF_MARKS):
        return False
    return any(w in t for w in _PRIVATE_WORDS)


@dataclass
class RelationEdge:
    """与一个人的关系边。全部字段可观测、可落盘。"""

    person: str
    topics: Set[str] = field(default_factory=set)   # 广度：聊过的话题
    private_n: int = 0                              # 深度：自述条数
    contacts: int = 0                               # 有效接触次数（消息条数）
    last_contact: float = 0.0                       # 最后接触时刻（秒）
    r: float = 0.0                                  # 关系强度 R ∈[0,1]，慢变量

    # ---------- 目标值 ----------
    def breadth(self) -> float:
        return 1.0 - math.exp(-len(self.topics) / BREADTH_SAT)

    def depth(self) -> float:
        return 1.0 - math.exp(-self.private_n / DEPTH_SAT)

    def r_target(self) -> float:
        """广度定上限、深度决定能不能接近上限。

        纯表面关系封顶在 SURFACE_FLOOR；深度饱和后到 1.0。这是"熟人 vs 朋友"
        在数值上的区别：认识很久但从不交心，R 到 0.4 就停。
        """
        return self.breadth() * (SURFACE_FLOOR + (1.0 - SURFACE_FLOOR) * self.depth())

    # ---------- 推进 ----------
    def contact(self, topics: Iterable[str], private_delta: int,
                now: float, k_a: float) -> None:
        """一次有效接触：更新计数，再把 R 朝目标推一步。"""
        self.contacts += 1
        self.last_contact = max(self.last_contact, now)
        for tp in topics:
            tp = (tp or "").strip()
            if 1 < len(tp) <= 12:
                self.topics.add(tp)
        if private_delta > 0:
            self.private_n += min(3, private_delta)   # 单条消息最多记 3 条自述
        self.r = clamp(self.r + k_a * (self.r_target() - self.r) * STEP, 0.0, 1.0)

    def decay(self, dt_h: float) -> None:
        if dt_h <= 0:
            return
        self.r *= 0.5 ** (dt_h / HALF_LIFE_H)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "person": self.person,
            "topics": sorted(self.topics),
            "private_n": self.private_n,
            "contacts": self.contacts,
            "last_contact": round(self.last_contact, 1),
            "r": round(self.r, 4),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RelationEdge":
        return cls(
            person=str(d.get("person", "")),
            topics=set(d.get("topics") or []),
            private_n=int(d.get("private_n", 0)),
            contacts=int(d.get("contacts", 0)),
            last_contact=float(d.get("last_contact", 0.0)),
            r=float(d.get("r", 0.0)),
        )


def k_of_agreeableness(a: float) -> float:
    """k_A = 0.5 + 0.5·A。与 (0.5+0.5·E) 同族，保证 A=0 时仍有一半推力。"""
    return K_A_BASE + (1.0 - K_A_BASE) * clamp(a, 0.0, 1.0)


class RelationshipMap:
    """多主体关系表。落盘 JSON，原子写，与 MindState 同一套写法。"""

    def __init__(self, path: str):
        self.path = path
        self.edges: Dict[str, RelationEdge] = {}
        self._last_decay_at = 0.0
        self.load()

    # ---------- 读写 ----------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for e in d.get("edges") or []:
            try:
                edge = RelationEdge.from_dict(e)
            except (TypeError, ValueError):
                continue
            if edge.person:
                self.edges[edge.person] = edge
        self._last_decay_at = float(d.get("last_decay_at", 0.0))

    def save(self) -> None:
        d = {
            "edges": [e.to_dict() for e in self.edges.values()],
            "last_decay_at": round(self._last_decay_at, 1),
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- 更新 ----------
    def observe(self, person: str, text: str, topics: Iterable[str],
                now: float, agreeableness: float) -> Optional[RelationEdge]:
        """一条来自某人的消息。person 为空时不分主体，直接返回 None。"""
        person = (person or "").strip()
        if not person:
            return None
        edge = self.edges.get(person)
        if edge is None:
            self._cap_size()
            edge = RelationEdge(person=person)
            self.edges[person] = edge
        private = 1 if _is_self_disclosure(text) else 0
        edge.contact(topics, private, now, k_of_agreeableness(agreeableness))
        return edge

    def decay_all(self, now: float) -> None:
        """按上次衰减时刻到现在的时间差衰减全部边；每 tick 调一次。"""
        if self._last_decay_at <= 0:
            # 首次调用：只记下起点，不衰减。用 1.0 兜底而不是 now——
            # now 若恰为 0 会被下一次的 <=0 判断再吃一遍，永远初始化不了
            self._last_decay_at = now if now > 0 else 1.0
            return
        # 单次衰减最多按 240h 计：正常 tick 每分钟一次，股份差远小于此；
        # 上限只在长时间停机后起作用，防止一次把关系抽干（30 天半衰期不等于
        # "停机 30 天关系归零"，停机期间系统也没在过日子）
        dt_h = clamp((now - self._last_decay_at) / 3600.0, 0.0, 240.0)
        self._last_decay_at = now
        if dt_h <= 0:
            return
        for edge in self.edges.values():
            edge.decay(dt_h)

    def _cap_size(self) -> None:
        if len(self.edges) < MAX_PERSONS:
            return
        weakest = min(self.edges.values(), key=lambda e: e.r)
        self.edges.pop(weakest.person, None)

    # ---------- 读出 ----------
    def strength(self, person: str) -> float:
        edge = self.edges.get(person or "")
        return edge.r if edge else 0.0

    def top(self, k: int = 5) -> list:
        return sorted(self.edges.values(), key=lambda e: e.r, reverse=True)[:k]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "n_persons": len(self.edges),
            "edges": [e.to_dict() for e in self.top(8)],
            "k_a_bounds": [K_A_BASE, 1.0],
            "half_life_h": HALF_LIFE_H,
        }
