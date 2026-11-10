# -*- coding: utf-8 -*-
"""识海记忆 v2：与心智 ODE 同源的分层记忆子系统。

设计边界
--------
- 不改动现有 ``modules/memory.py`` 的读写接口，旧链路继续跑（消融 C 组基线）。
- 本模块是并行新链路（消融 D / C' 组），由心智 tick / 情绪事件钩子显式调用。
- 记忆不是 RAG：检索结果先更新激活 a(t)、情绪强迫项与念头基线，
  只有行为层决定“要说出来”时才把摘要拼进 prompt。

三层
----
1. 工作记忆：仍由 AstrBot 会话上下文负责，本模块不重复存。
2. 事实层 fact.json：稳定身份/关系/约定，慢衰减，少量常驻。
3. 情景层 event 快照：带情绪峰值的事件摘要，按需检索，默认不进 prompt。
   原文由 ``doc_id`` 指向 livingmemory.documents.text，需要时才回查。

时间约定
--------
所有衰减都是连续时间指数，落盘只记权重与 last_update_ts；
每次 tick 只按 Δt（小时）乘 exp(-α·Δt)，因此 10min / 30min / 60min
步长得到同一条曲线，步长无关。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 参数（论文可直接引用；α 由半衰期/日衰减率换算，单位 h^-1）
# ---------------------------------------------------------------------------
def _alpha_from_half_life(days: float) -> float:
    """α = ln2 / (半衰期·24h)，单位 h^-1。按"半衰期"标定，比写日衰减率直观。"""
    return math.log(2.0) / (float(days) * 24.0)


# 分桶标定（长期陪伴口径，不是短期工作记忆）：
#   事实 15 天 —— 半个月不提就淡出（"上周末说的事这周三还想得起，但一个月前的细枝末节可以糊")
#   事件 30 天 —— 带情境，比纯事实更耐久
#   核心 90 天 —— 高光/深度绑定情绪的那类，季度级
ALPHA_FACT_NORMAL = _alpha_from_half_life(15)   # ≈ 1.93e-3 h^-1
ALPHA_FACT_CORE = _alpha_from_half_life(90)     # ≈ 3.21e-4 h^-1
ALPHA_EVENT = _alpha_from_half_life(30)         # ≈ 9.63e-4 h^-1

EPS_DE_DT = 0.30              # |de/dt| 情绪拐点阈值
U_PEAK_THRESH = 2.0            # 念头局部峰值（低于行为阈值 2.1）
CONFLICT_GAP_LOCK = 0.30       # 权重差超过此值进入倾向锁定
CONFLICT_GAP_UNLOCK = 0.15     # 权重差回落到此值以内解锁
LOCK_BOOST_SCALE = 0.5         # 锁定后落后事实抬权倍率
LOCK_SECONDS = 7 * 86400       # 倾向锁定观察窗

# 检索预算（§12.5 省 Token：可在插件配置 memory.fact_top_n / event_top_n /
# event_content_max 覆盖）。旧值 4/3/220 每轮注入约 900 字，压到 3/2/120 后约 400 字。
FACT_TOP_N = 3
EVENT_TOP_N = 2
EVENT_CONTENT_MAX = 120
# 召回门槛：得分低于门槛的记忆不进提示词（无关旧闻不占预算）。
# 事实分 = 相关重合×2 + 权重×0.6 + 核心 0.15 + imp×0.35
# 事件分 = 相关重合×2 + 激活×0.8 + 0.10 + imp×0.35
MIN_FACT_SCORE = 0.30
MIN_EVENT_SCORE = 0.35
# 重要度调制（§5.4 扩展）：imp 由固结 LLM 自评直接生效（规则仅兜底，不做 max）。
# 生效点：λ_eff = λ/(1+IMP_BETA·imp) 衰减 + 检索排序 +0.35·imp。
# 默认 0.0 = 尚未评分不调制（存量数据加载后寿命不变，等固结/回填再产生区分度）。
IMP_BETA = 2.0
# 艾宾浩斯稳定度 S（R = e^{-Δt/S}）：λ_eff = λ_base / S。
# 原来只有 imp 调制（λ/(1+β·imp)），"被反复想起"和"情绪有多重"都不进分母，
# 所以同一件老事被翻出来多少次都一样快忘——复习（Recall）不会让曲线变平。
# S = (1+β·imp)·(1+α_E·E_v)·(1+β_N·N_recall)：
#   E_v 事件取 peak_u（冻结时的冲动峰值），事实用 imp 代理；
#   N_recall 每被召回一次 +1（上限 max，防止无限加固成僵尸记忆）。
STAB_EMO_ALPHA = 2.0
STAB_RECALL_BETA = 1.5
STAB_RECALL_MAX = 12
# 复习计分的防作弊门槛（间隔重复）：
#   1) 只在"核心实体真的有重合"时才算复习——"吃了吗"这种废话召回不算；
#   2) 同一件事 24h 内被连问三次也只算一次复习，真正的复习必须跨时间窗。
RECALL_MIN_OVERLAP = 2
RECALL_MIN_GAP = 24 * 3600
# 事件落盘截断上限（保细节；注入时才按 memory.event_content_max 截短）
EVENT_STORE_MAX = 220
# 事件摘要质量闸阈值：含换行 / 以「用户：」「小助手：」（旧转写的「用户：」「我：」）开头的原文
# → degraded（可检索但不进注入）。阈值与固结 prompt 的「不超过100字」契约对齐；
# 定在 80 会把 85~100 字的合格摘要一起误伤成"不进注入"。
EVENT_DEGRADE_MAX = 100

# 转写原文的说话人前缀（新旧称呼都算原文，避免改名后旧条目漏进注入）
DIALOGUE_PREFIXES = ("用户：", "小助手：", "用户：", "我：")

# 事件检索里 a（freeze_peak 冻结的峰值，只降不升）的权重。
# 0.8 时旧事件靠 a=1.0 拿到 ~0.9 的底分，任何 query 都进 top_n；
# 压到 0.35 后字面重合主导，"日子"改由 timeline() 兜底。
EVENT_A_WEIGHT = 0.35
# 时间线注入：最近多少天（与 memory.TIMELINE_TIERS 的最后一档对齐：近半月）
TIMELINE_DAYS = 15
# 无价值事件的归档阈值：超过这么多天、又永远进不了注入的（degraded 且无 summary，
# 或 imp 极低且几乎没被激活过），从可注入池移出去，落到 event_archive.json。
# 只归档、不物理删除——原文还在 messages.db，这里只是让它不再参与检索与遍历。
EVENT_PRUNE_DAYS = 15
# 归档的艾宾浩斯判据：保持率 R 跌破这个比例 = 这条已经想不起来了
EVENT_PRUNE_RETENTION = 0.35


def is_degraded_content(content: str) -> bool:
    """原文兜底/超长摘要判定：在线固结与 --degrade-legacy 共用同一口径。"""
    c = str(content or "")
    return (
        "\n" in c
        or len(c) > EVENT_DEGRADE_MAX
        or c.lstrip().startswith(DIALOGUE_PREFIXES)
    )


def _now() -> int:
    return int(time.time())


def exp_decay(value: float, alpha: float, delta_hours: float) -> float:
    """w(t+Δt) = w(t) * exp(-α·Δt)，Δt 单位为小时。"""
    if delta_hours <= 0:
        return float(value)
    return float(value) * math.exp(-float(alpha) * float(delta_hours))


def grams(text: str) -> set[str]:
    """零依赖字符二元组，和现有 memory.py 的粗相关度保持一致。"""
    clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
@dataclass
class FactItem:
    fact_id: str
    subject: str
    predicate: str
    obj: str
    weight: float = 0.5
    decay_factor: float = 0.992
    expire_at: int = 0
    last_update_ts: int = 0
    conflict_group: Optional[str] = None
    invariant_bind: List[str] = field(default_factory=list)
    source_doc_id: Optional[str] = None
    is_core: bool = False
    lock_until_ts: int = 0
    lead_margin: float = 0.0
    imp: float = 0.0              # LLM 自评重要度（0-1，一位小数）；0.0=尚未评分不调制
    recalls: int = 0              # 被重新想起的次数（复习效应：N 越大 S 越大，衰减越慢）
    last_recall_ts: int = 0       # 上一次算作"复习"的时间（防同一轮里连问三次刷分）

    def stability(self) -> float:
        """艾宾浩斯 S = (1+β·imp)·(1+β_N·N_recall)。

        事实没有独立情绪强度字段，imp 就是它的"有多重"，所以不再重复乘 E_v。"""
        imp = min(1.0, max(0.0, float(self.imp or 0)))
        n = min(STAB_RECALL_MAX, max(0, int(self.recalls or 0)))
        return (1.0 + IMP_BETA * imp) * (1.0 + STAB_RECALL_BETA * n)

    def retention(self, now_ts: int) -> float:
        """保持率 R = e^{-Δt/S}（归档判据：R 跌破阈值 = 这条已经想不起来了）。"""
        last = int(self.last_update_ts or now_ts)
        dt_h = max(0.0, (now_ts - last) / 3600.0)
        alpha = ALPHA_FACT_CORE if self.is_core else ALPHA_FACT_NORMAL
        return math.exp(-(alpha / self.stability()) * dt_h)

    def decay_to(self, now_ts: int) -> None:
        if not self.last_update_ts:
            self.last_update_ts = now_ts
            return
        dt_h = max(0.0, (now_ts - self.last_update_ts) / 3600.0)
        alpha = ALPHA_FACT_CORE if self.is_core else ALPHA_FACT_NORMAL
        # λ_eff = λ/S：imp 越重、被想起越多次，S 越大，忘得越慢
        alpha = alpha / self.stability()
        self.weight = exp_decay(self.weight, alpha, dt_h)
        self.last_update_ts = now_ts


@dataclass
class EventItem:
    node_id: str
    status: str = "pending"       # pending -> committed
    frozen_at: int = 0
    ts: int = 0
    content: str = ""
    a: float = 0.0
    decay_factor: float = 0.996
    expire_at: int = 0
    de_dt: float = 0.0
    peak_u: float = 0.0
    doc_id: Optional[str] = None
    emotion_tag: Optional[str] = None
    edge_kw: List[str] = field(default_factory=list)
    edge_emotion: List[str] = field(default_factory=list)
    last_update_ts: int = 0
    imp: float = 0.0              # LLM 自评重要度（0-1，一位小数）；0.0=尚未评分不调制
    degraded: bool = False        # 质量闸：原文兜底/超长摘要 → 可检索但不进注入
    summary: str = ""             # 时间线用的一句话压缩（原文保留在 content，不动它）
                                  # 有 summary 的 degraded 事件可以进时间线
    recalls: int = 0              # 被重新想起的次数（复习效应）
    last_recall_ts: int = 0       # 上一次算作"复习"的时间

    def stability(self) -> float:
        """艾宾浩斯 S = (1+β·imp)·(1+α_E·E_v)·(1+β_N·N_recall)。

        E_v 用 peak_u（冻结那一刻的冲动峰值）：吵架、崩溃这类峰值高的事件
        S 天然更大，隔很久也还带着温度；平静的日常峰值低，忘得快。"""
        imp = min(1.0, max(0.0, float(self.imp or 0)))
        e_v = min(1.0, max(0.0, float(self.peak_u or 0)))
        n = min(STAB_RECALL_MAX, max(0, int(self.recalls or 0)))
        return ((1.0 + IMP_BETA * imp)
                * (1.0 + STAB_EMO_ALPHA * e_v)
                * (1.0 + STAB_RECALL_BETA * n))

    def retention(self, now_ts: int) -> float:
        """保持率 R = e^{-Δt/S}（归档判据）。"""
        last = int(self.last_update_ts or now_ts)
        dt_h = max(0.0, (now_ts - last) / 3600.0)
        return math.exp(-(ALPHA_EVENT / self.stability()) * dt_h)

    def decay_to(self, now_ts: int) -> None:
        if not self.last_update_ts:
            self.last_update_ts = now_ts
            return
        dt_h = max(0.0, (now_ts - self.last_update_ts) / 3600.0)
        # λ_eff = λ/S（§5.2 改写：S 越大忘得越慢，不再是只看 imp 的常数）
        alpha = ALPHA_EVENT / self.stability()
        self.a = exp_decay(self.a, alpha, dt_h)
        self.last_update_ts = now_ts


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
class _JsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> List[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def save(self, rows: Iterable[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(list(rows), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


class FactBank:
    """事实层：稳定事实、冲突博弈、倾向锁定、慢衰减。"""

    def __init__(self, path: Path):
        self.store = _JsonStore(path)
        self.items: Dict[str, FactItem] = {}
        for row in self.store.load():
            try:
                self.items[row["fact_id"]] = FactItem(**row)
            except TypeError:
                continue

    def save(self) -> None:
        self.store.save(asdict(v) for v in self.items.values())

    def tick(self, now_ts: Optional[int] = None) -> None:
        now_ts = now_ts or _now()
        for it in self.items.values():
            it.decay_to(now_ts)
        self.save()

    def add(self, item: FactItem, save: bool = True) -> Tuple[str, bool]:
        """写入或合并。返回 (fact_id, 是否新增)。save=False 供批量写入路径使用。"""
        item.weight = min(max(float(item.weight), 0.0), 1.0)
        item.last_update_ts = item.last_update_ts or _now()
        old = self._find_similar(item)
        if old is not None:
            old.weight = min(1.0, old.weight + 0.10)
            old.last_update_ts = item.last_update_ts
            if save:
                self.save()
            return old.fact_id, False
        self.items[item.fact_id] = item
        self._apply_conflict(item)
        if save:
            self.save()
        return item.fact_id, True

    def _find_similar(self, new: FactItem) -> Optional[FactItem]:
        key = f"{new.subject}|{new.predicate}|{new.obj}"
        for old in self.items.values():
            old_key = f"{old.subject}|{old.predicate}|{old.obj}"
            if key == old_key:
                return old
            if new.conflict_group and old.conflict_group == new.conflict_group:
                # 同一冲突组且 object 高度重合，视为同一事实的不同版本
                if new.obj in old.obj or old.obj in new.obj:
                    return old
        return None

    def _apply_conflict(self, new: FactItem) -> None:
        """冲突组内的权重博弈，带倾向锁定，避免人格在两条事实间抽搐。"""
        if not new.conflict_group:
            return
        group = [x for x in self.items.values() if x.conflict_group == new.conflict_group]
        if len(group) < 2:
            return
        # 新版本进入时，旧版本先降权；新版本初始权重压低
        for old in group:
            if old.fact_id != new.fact_id:
                old.weight = max(0.0, old.weight * 0.8)
        new.weight = min(new.weight, 0.45)
        self._recompute_lock(group)

    def _recompute_lock(self, group: List[FactItem]) -> None:
        """按当前权重差重算锁定；差距回到 UNLOCK 内就解锁。"""
        if len(group) < 2:
            return
        leader, second = sorted(group, key=lambda x: x.weight, reverse=True)[:2]
        gap = max(0.0, leader.weight - second.weight)
        leader.lead_margin = gap
        if gap >= CONFLICT_GAP_LOCK:
            second.lock_until_ts = _now() + LOCK_SECONDS
        elif gap <= CONFLICT_GAP_UNLOCK:
            for it in group:
                it.lock_until_ts = 0

    def boost(self, fact_id: str, delta: float = 0.08, now_ts: Optional[int] = None,
              save: bool = True) -> None:
        now_ts = now_ts or _now()
        it = self.items.get(fact_id)
        if it is None:
            return
        if it.lock_until_ts > now_ts:
            delta *= LOCK_BOOST_SCALE
        it.weight = min(1.0, it.weight + delta)
        it.last_update_ts = now_ts
        if it.conflict_group:
            group = [x for x in self.items.values() if x.conflict_group == it.conflict_group]
            self._recompute_lock(group)
        if save:
            self.save()

    def retrieve(self, query: str, top_n: int = FACT_TOP_N,
                 min_score: float = 0.0) -> List[FactItem]:
        """事实层检索：相关度优先，权重兜底；核心事实只在小名额里保留。
        min_score：召回门槛（低于门槛不进提示词，省 Token）。
        imp 以乘性参与排序（与相关度耦合，不再是常数奖励）。"""
        q = grams(query)

        def score(it: FactItem) -> float:
            text = f"{it.subject}{it.predicate}{it.obj}"
            overlap = len(q & grams(text)) if q else 0
            # 乘性微调（0.8+0.2·imp）：未评分 0.8 折、评过"闲聊"(0.2) 0.84 折——
            # "没打分"不能比"判为闲聊"更惨；同时这是温和调制，不会把
            # MIN_*_SCORE 门槛悄悄收紧一截（0.6+0.4·imp 时事实门槛 0.30→w≥0.83）。
            base = overlap * 2.0 + it.weight * 0.6 + (0.15 if it.is_core else 0.0)
            return base * (0.8 + 0.2 * min(1.0, max(0.0, it.imp)))

        rows = sorted(self.items.values(), key=score, reverse=True)[: max(1, top_n)]
        out = [it for it in rows if score(it) >= float(min_score)]
        # 复习效应（带两条防作弊）：核心实体重合 ≥2 个二元组、且距上次复习
        # 已过 24h，才让 S 涨一档。只动 recalls / last_recall_ts，不动 weight。
        now_ts = _now()
        for it in out:
            overlap = len(q & grams(f"{it.subject}{it.predicate}{it.obj}")) if q else 0
            if overlap < RECALL_MIN_OVERLAP:
                continue
            if now_ts - int(it.last_recall_ts or 0) < RECALL_MIN_GAP:
                continue
            it.recalls = min(STAB_RECALL_MAX, int(it.recalls or 0) + 1)
            it.last_recall_ts = now_ts
            it.last_update_ts = now_ts
        return out


class EventBank:
    """情景层：双阶段固结、事件优先检索、原文指针。"""

    def __init__(self, path: Path):
        self.store = _JsonStore(path)
        # 归档区：无价值的老事件移到这里（只移不删），检索/时间线不再遍历
        self.archive = _JsonStore(Path(path).with_name("event_archive.json"))
        self._last_sweep: int = 0
        self.items: Dict[str, EventItem] = {}
        for row in self.store.load():
            try:
                self.items[row["node_id"]] = EventItem(**row)
            except TypeError:
                continue

    def save(self) -> None:
        self.store.save(asdict(v) for v in self.items.values())

    def tick(self, now_ts: Optional[int] = None) -> None:
        now_ts = now_ts or _now()
        for it in self.items.values():
            it.decay_to(now_ts)
        self.save()
        # 归档不在每次 tick 跑：只在 3 / 7 / 15 天这三个"体检日"跑（每天最多一次）
        self.archive_stale(now_ts=now_ts)

    def archive_stale(self, days: int = EVENT_PRUNE_DAYS,
                      min_retention: float = EVENT_PRUNE_RETENTION,
                      now_ts: Optional[int] = None) -> int:
        """按"体检日"归档：3 天 / 7 天 / 15 天各查一次，不是每天都查。

        判据（已固结、且本身价值低：degraded 无 summary，或 imp≤0.2 且 a<0.15）：
          · 3 天体检：R < 0.35 → 归档（短期不活跃的清理）
          · 7 天体检：R < 0.30 → 归档（放宽一档）
          · ≥15 天   ：硬淘汰（终局关卡，之后任意一天都可能被清）
        只在这三个时点上判：一条记忆若在 3/7 天时还"想起来过"（raises S），
        就会活到 15 天那关；15 天还没死的，说明它真被反复复习过，长期保留。
        每天最多扫一次，避免 tick 频繁搬运文件。"""
        now_ts = int(now_ts or _now())
        if now_ts - int(self._last_sweep or 0) < 86400:
            return 0
        self._last_sweep = now_ts
        stale: List[EventItem] = []
        for ev in self.items.values():
            if ev.status != "committed":
                continue
            dead_text = bool(ev.degraded) and not (ev.summary or "").strip()
            dead_weight = float(ev.imp or 0) <= 0.2 and float(ev.a or 0) < 0.15
            if not (dead_text or dead_weight):
                continue
            age_days = (now_ts - int(ev.ts or 0)) / 86400.0
            r = ev.retention(now_ts)
            if age_days >= EVENT_PRUNE_DAYS:
                stale.append(ev)                      # 15 天硬淘汰
            elif 7.0 <= age_days < 7.0 + 1.0:
                if r < min_retention * 0.86:           # 7 天体检（0.30）
                    stale.append(ev)
            elif 3.0 <= age_days < 3.0 + 1.0:
                if r < min_retention:                  # 3 天体检（0.35）
                    stale.append(ev)
        if not stale:
            return 0
        rows = self.archive.load()
        rows.extend(asdict(v) for v in stale)
        self.archive.save(rows)
        for ev in stale:
            self.items.pop(ev.node_id, None)
        self.save()
        return len(stale)

    def freeze_peak(
        self,
        *,
        ts: int,
        de_dt: float,
        peak_u: float,
        e_t: float,
        doc_id: Optional[str] = None,
    ) -> str:
        """阶段一：同步冻结峰值。不调 LLM，不等待任何异步结果。"""
        node_id = f"event:{int(ts * 1000)}"
        old = self.items.get(node_id)
        if old is None:
            old = EventItem(
                node_id=node_id,
                status="pending",
                frozen_at=int(ts),
                ts=int(ts),
                content="",
                a=float(e_t),
                de_dt=float(de_dt),
                peak_u=float(peak_u),
                doc_id=doc_id,
                last_update_ts=int(ts),
            )
            self.items[node_id] = old
        else:
            old.de_dt = float(de_dt)
            old.peak_u = max(old.peak_u, float(peak_u))
            old.a = max(old.a, float(e_t))
            if doc_id and not old.doc_id:
                old.doc_id = doc_id
        self.save()
        return node_id

    def commit(
        self,
        node_id: str,
        *,
        content: str,
        edge_kw: Optional[List[str]] = None,
        edge_emotion: Optional[List[str]] = None,
        emotion_tag: Optional[str] = None,
        imp: Optional[float] = None,
        degraded: bool = False,
    ) -> bool:
        """阶段二：异步摘要/实体/边/重要度写回；不改变已冻结的 de_dt/peak_u。"""
        ev = self.items.get(node_id)
        if ev is None or ev.status != "pending":
            return False
        ev.content = str(content or "")[:EVENT_STORE_MAX]
        ev.edge_kw = list(edge_kw or [])
        ev.edge_emotion = list(edge_emotion or [])
        ev.emotion_tag = emotion_tag
        if imp is not None:
            ev.imp = round(min(1.0, max(0.0, float(imp))), 1)
        ev.degraded = bool(degraded)
        ev.status = "committed"
        self.save()
        return True

    def boost(self, node_id: str, delta: float = 0.12, now_ts: Optional[int] = None,
              save: bool = True) -> None:
        now_ts = now_ts or _now()
        ev = self.items.get(node_id)
        if ev is None:
            return
        ev.a = min(1.0, ev.a + delta)
        ev.last_update_ts = now_ts
        if save:
            self.save()

    def retrieve(self, query: str, top_n: int = EVENT_TOP_N,
                 min_score: float = 0.0) -> List[EventItem]:
        """事件优先；只取 committed，pending 不拿去做表达。
        min_score：召回门槛（久远且无相关的旧事件不进提示词，省 Token）。
        imp 以乘性参与排序（与相关度耦合，不再是常数奖励）。"""
        q = grams(query)

        def score(ev: EventItem) -> float:
            text = f"{ev.content} {' '.join(ev.edge_kw)}"
            overlap = len(q & grams(text)) if q else 0
            # 乘性微调（0.8+0.2·imp），语义同 FactBank.retrieve：未评分不重罚。
            # a 系数 0.35：a 是 freeze_peak 冻结的峰值、只降不升，旧事件能长期
            # 靠 a=1.0 拿到 ~0.4 的底分，任何 query 都挤进 top_n。压低后
            # 字面重合（overlap·2.0）重新主导，"日子"靠 timeline() 单独兜。
            base = overlap * 2.0 + ev.a * EVENT_A_WEIGHT + 0.10
            return base * (0.8 + 0.2 * min(1.0, max(0.0, ev.imp)))

        rows = sorted((e for e in self.items.values() if e.status == "committed"),
                      key=score, reverse=True)[: max(1, top_n)]
        out = [ev for ev in rows if score(ev) >= float(min_score)]
        # 复习效应（同 FactBank：实体重合 ≥2 且跨过 24h 时间阀才算一次复习）
        now_ts = _now()
        for ev in out:
            text = f"{ev.content} {' '.join(ev.edge_kw)}"
            overlap = len(q & grams(text)) if q else 0
            if overlap < RECALL_MIN_OVERLAP:
                continue
            if now_ts - int(ev.last_recall_ts or 0) < RECALL_MIN_GAP:
                continue
            ev.recalls = min(STAB_RECALL_MAX, int(ev.recalls or 0) + 1)
            ev.last_recall_ts = now_ts
            ev.last_update_ts = now_ts
        return out

    def timeline(self, days: int = TIMELINE_DAYS, per_day: int = 1,
                 now_ts: Optional[int] = None) -> List[EventItem]:
        """时间线兜底：最近 days 天，每天最多 per_day 条可注入事件。

        与 query 无关——补的是"日子"这一维，避免 event_top_n 被 a 高的
        老事件长期独占。同一天内按 imp → a → ts 依次取最高。
        """
        now_ts = int(now_ts or _now())
        today = datetime.fromtimestamp(now_ts).date()
        by_day: Dict[int, List[EventItem]] = {}
        for ev in self.items.values():
            if ev.status != "committed":
                continue
            text = (ev.summary or "").strip()
            if not text:
                if not ev.content or is_degraded_content(ev.content):
                    continue
                text = (ev.content or "").strip()
            d = datetime.fromtimestamp(ev.ts).date()
            delta = (today - d).days
            if delta < 0 or delta >= days:
                continue
            by_day.setdefault(delta, []).append(ev)
        out: List[EventItem] = []
        for delta in sorted(by_day):
            rows = sorted(by_day[delta],
                          # 同一天内先按 imp（取到一位小数，别让 0.85/0.9 的浮点
                          # 差把当天主线挤掉），再按 ts **升序**——一天里最早的
                          # 那条通常是主线（"早上八点考试"），取最新容易抽到收尾碎话。
                          key=lambda e: (-round(float(e.imp or 0), 1), int(e.ts or 0)))
            out.extend(rows[: max(1, per_day)])
        return out


# ---------------------------------------------------------------------------
# 顶层：与心智 ODE 对接的接口
# ---------------------------------------------------------------------------
class MemoryV2:
    """心智侧唯一入口。所有方法都不应阻塞主对话循环。"""

    def __init__(self, base_dir: str | Path):
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        self.facts = FactBank(base / "fact.json")
        self.events = EventBank(base / "event_snapshot.json")

    # （旧 MemoryV2.tick(delta_hours) 已删除：它是唯一不带 imp 调制的衰减入口，
    #   留着就是"谁调谁绕过 λ_eff"；统一走 FactBank/EventBank.tick(now_ts)。）

    # ---- 阶段一：ODE 事件检测直接调用，同步冻结 ----
    def on_ode_peak(
        self,
        *,
        de_dt: float,
        peak_u: float,
        e_t: float,
        ts: Optional[int] = None,
        doc_id: Optional[str] = None,
        force: bool = False,
    ) -> Optional[str]:
        """满足 |de/dt| 或 u 峰值时冻结；返回 pending node_id。"""
        if not force and abs(de_dt) < EPS_DE_DT and peak_u < U_PEAK_THRESH:
            return None
        ts = int(ts or _now())
        node_id = self.events.freeze_peak(
            ts=ts, de_dt=de_dt, peak_u=peak_u, e_t=e_t, doc_id=doc_id
        )
        # 调用方在这里同步做：graph.add_pending_event(node_id, ...)
        # 以及情绪场强迫项 mind.add_memory_forcing(event_items)
        return node_id

    # ---- 阶段二：异步任务，不阻塞峰值 ----
    def commit_pending(
        self,
        node_id: str,
        *,
        content: str,
        edge_kw: Optional[List[str]] = None,
        edge_emotion: Optional[List[str]] = None,
        emotion_tag: Optional[str] = None,
        imp: Optional[float] = None,
        degraded: bool = False,
    ) -> bool:
        return self.events.commit(
            node_id,
            content=content,
            edge_kw=edge_kw,
            edge_emotion=edge_emotion,
            emotion_tag=emotion_tag,
            imp=imp,
            degraded=degraded,
        )

    # ---- 检索：先更新激活，再由行为层决定是否拼 prompt ----
    def recall(self, query: str, fact_top_n: int = FACT_TOP_N,
               event_top_n: int = EVENT_TOP_N) -> Tuple[List[FactItem], List[EventItem]]:
        facts = self.facts.retrieve(query, fact_top_n, MIN_FACT_SCORE)
        events = self.events.retrieve(query, event_top_n, MIN_EVENT_SCORE)
        # 批量 boost 后各落盘一次（注入钩子里逐条 save 会放大同步 IO）
        for it in facts:
            self.facts.boost(it.fact_id, 0.04, save=False)
        for ev in events:
            self.events.boost(ev.node_id, 0.08, save=False)
        if facts:
            self.facts.save()
        if events:
            self.events.save()
        return facts, events

    def prompt_block(self, facts: List[FactItem], events: List[EventItem]) -> str:
        """只在行为层决定开口后调用；未调用则这些记忆不占 prompt token。"""
        lines: List[str] = []
        for it in facts:
            lines.append(f"- {it.subject}{it.predicate}：{it.obj}")
        for ev in events:
            if ev.content:
                lines.append(f"- {ev.content[:EVENT_CONTENT_MAX]}")
        return "\n".join(lines)

    # ---- 冲突写入：供 EgoRegulator / GuardAuditor 调用 ----
    def add_fact(
        self,
        *,
        subject: str,
        predicate: str,
        obj: str,
        weight: float = 0.5,
        conflict_group: Optional[str] = None,
        invariant_bind: Optional[List[str]] = None,
        source_doc_id: Optional[str] = None,
        is_core: bool = False,
        imp: Optional[float] = None,
        save: bool = True,
    ) -> Tuple[str, bool]:
        fact_id = "fact:" + hashlib.sha1(
            f"{subject}|{predicate}|{obj}".encode("utf-8")
        ).hexdigest()[:16]
        item = FactItem(
            fact_id=fact_id,
            subject=str(subject),
            predicate=str(predicate),
            obj=str(obj),
            weight=float(weight),
            decay_factor=0.998 if is_core else 0.992,
            last_update_ts=_now(),
            conflict_group=conflict_group,
            invariant_bind=list(invariant_bind or []),
            source_doc_id=source_doc_id,
            is_core=bool(is_core),
            imp=round(min(1.0, max(0.0, float(imp))), 1) if imp is not None else 0.0,
        )
        fid, added = self.facts.add(item, save=save)
        if not added and imp is not None:
            # 合并命中旧事实：重要度只升不降（"下周面试"0.9 不被闲聊重记降为 0.2）
            old = self.facts.items.get(fid)
            if old is not None:
                old.imp = round(max(float(old.imp), min(1.0, max(0.0, float(imp)))), 1)
        return fid, added


# ---------------------------------------------------------------------------
# 自检 / 存量回填（不依赖 AstrBot，可在命令行直接跑）
# ---------------------------------------------------------------------------
def _backfill_imp(root: Path, force: bool = False) -> None:
    """存量 imp 回填：规则三档 0.9/0.4/0.2（与 memory.py 规则兜底同口径）。

    强事 0.9（约定/生病/考试/身份…）、计划 0.4（明天/约好/记得…）、其余 0.2。
    幂等：已有评分（imp>0，含 LLM 自评）的条目跳过，--force 才覆盖。
    跑完打印分布（目标均值 ~0.4、≥0.8 占比 <15%）。"""
    strong = re.compile(
        r"(约定|答应|承诺|生日|生病|吃药|考试|面试|还钱|借钱|身份证|全名|我叫)")
    plan = re.compile(r"(明天|今晚|晚上|周末|下周|约好|记得|别忘了|一起|准备|要去)")
    mem = MemoryV2(root)
    vals: list[float] = []
    skipped = 0
    for it in mem.facts.items.values():
        if not force and float(it.imp or 0) > 0:
            skipped += 1
            continue
        t = f"{it.subject}{it.predicate}{it.obj}"
        it.imp = 0.9 if strong.search(t) else (0.4 if plan.search(t) else 0.2)
        vals.append(it.imp)
    for ev in mem.events.items.values():
        if not force and float(ev.imp or 0) > 0:
            skipped += 1
            continue
        t = f"{ev.content} {' '.join(ev.edge_kw)}"
        ev.imp = 0.9 if strong.search(t) else (0.4 if plan.search(t) else 0.2)
        vals.append(ev.imp)
    mem.facts.save()
    mem.events.save()
    n = len(vals)
    hi = sum(1 for v in vals if v >= 0.8)
    print(f"backfilled {n} items, skipped {skipped} already-scored"
          f" (fact {len(mem.facts.items)} / event {len(mem.events.items)})")
    if n:
        print(f"imp mean={sum(vals) / n:.3f}  >=0.8: {hi} ({100.0 * hi / n:.1f}%)")
        for v in (0.9, 0.4, 0.2):
            print(f"  {v}: {sum(1 for x in vals if abs(x - v) < 1e-6)}")


def _degrade_legacy(root: Path) -> None:
    """存量原文兜底事件打 degraded：按 is_degraded_content 统一口径重算（可升可降）。

    11-08 前后固结 LLM 100% 失败期间，raw 对话原文被当摘要写进了
    event_snapshot.json；这些行经"近 12h 豁免"漏进每轮 prompt。

    注意（写盘竞态）：本函数会整库 save()。若 AstrBot 正在运行，它的
    100s~300s tick 也会 save() 同一个 JSON，后写者覆盖先写者——曾出现
    "CLI 标记 380 条、运行实例 tick 落盘后变回 0"。
    跑之前先停 watchdog 并结束 AstrBot：`schtasks /end BotAstrBot`，
    跑完再启动。
    """
    mem = MemoryV2(root)
    n = 0
    un = 0
    for ev in mem.events.items.values():
        c = ev.content or ""
        want = is_degraded_content(c)
        if want and not bool(ev.degraded):
            ev.degraded = True
            n += 1
        elif not want and bool(ev.degraded):
            # 旧口径（>80 字）误伤的合格摘要（85~100 字、单行）重新放回注入
            ev.degraded = False
            un += 1
    mem.events.save()
    print(f"marked {n} degraded, unmarked {un} of {len(mem.events.items)} events")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3] / "plugin_data" / "astrbot_plugin_qq_toolbox" / "memory_v2"
    if "--backfill-imp" in sys.argv:
        _backfill_imp(root, force="--force" in sys.argv)
    elif "--degrade-legacy" in sys.argv:
        _degrade_legacy(root)
    else:
        mem = MemoryV2(root)
        print("fact_n", len(mem.facts.items), "event_n", len(mem.events.items))
        print("alpha", ALPHA_FACT_NORMAL, ALPHA_FACT_CORE, ALPHA_EVENT)
        print("decay_1h", exp_decay(1.0, ALPHA_FACT_NORMAL, 1.0))
        print("decay_10m", exp_decay(1.0, ALPHA_FACT_NORMAL, 1.0 / 6.0))
        print("decay_60m", exp_decay(1.0, ALPHA_FACT_NORMAL, 1.0))
