"""识海神经图谱（论文 §5.2/5.3/5.4/5.5、§7.2/7.3）：类型化有向图 + 扩散激活。

为什么不用向量搜索（§7.1）：扁平向量库只能找"相似的句子"；类型化图谱沿突触
扩散，能串起没有字面重叠的概念（"饿"→【食物】【中午】【疲惫】→ 昨天的
"想吃肯德基"）。全程零 LLM 调用（§问题定义 d）。

方程：
    扩散   a(v,t+Δt) = (1-λ_v(e))·a(v,t) + Σ_{u∈N(v)} M(u,v)·a(u,t)/d(u)   (5.3)
    衰减   λ_v(e) = λ_type / (1 + α·e_dom·E_v)                            (5.4)
    Hebbian M(u,v) ← M(u,v) + γ·𝟙[a(u)>θ_act]·𝟙[a(v)>θ_act]              (5.5)
    注入   s_int,i = Σ_{v∈Recall(G,k)} a(v,t)·E_{v,i}                      (5.2)

节点类型与基础衰减率（每小时，§7.2）：
    fact 0.1（细节会忘） | emotion 0.01（情感印象长存）
    event 0.05（模糊保留）| highlight 0.005（高光/创伤难忘）
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import jieba
    try:
        jieba.setLogLevel(60)  # 关闭载入日志
    except Exception:
        pass
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover - jieba 缺失时退化为连续切片
    _HAS_JIEBA = False

from .emotion import LAMBDA_TYPE
from .state import EMOTION_DIMS, clamp

STOPWORDS = set(
    # 注意：这里必须用空格分隔的"词"，不能整段无空格拼接——否则 split() 只得到
    # 一个巨型 token，中文停用词全部失效（历史 bug：kw:觉得/已经/明天 等垃圾节点霸榜）
    "已经 正在 一下 没有 是不是 现在 什么时候 怎么 为什么 这样 那样 自己 东西 事情 感觉 觉得 知道"
    " 出来 起来 过来 下去 上来 回去 有些 一点 非常 真的 直接 今天 明天 昨天 刚才 等等 而且 或者 除了"
    " 时间 剩下 注意 记得 之后 以前 以后 上次 刚刚 其实 应该 可能 需要 开始 最后 问题 地方 工作"
    " 内容 消息 状态 参数 配置 主动 回复 请求 适合 有趣 黄脸 语音 重新 只是 还有 而是 对话 聊天 记录"
    " 系统 框架 模型 提示 工具 调用 功能 插件 服务 账号 主人 日志 测试"
    " 明早 明晚 明日 今早 今晚 昨晚 待会 回头 下次 改天 周末 等会儿"
    " the a an is are was were be to of and in on it this that i you he she we they"
    " for with not have do does did so but at as or if".split()
)

_PROMISE_RX = re.compile(r"(答应|承诺|明天|下次|改天|回头|周末|约定|记得|到时候|说了要)")
_FAKE_PROMISE_RX = re.compile(r"记得吗|还记得|你是谁|说过吗")  # 疑问句不是承诺，别当开口由头
_PRUNE_RX = re.compile(r"[\s\d\W_]+$")
# 指代未来的精确时间词：含义随日期飘移，锚点过期后绝不能再当"未来的事"问（考试事故的根源）
_DEICTIC_TOMORROW_RX = re.compile(r"明天|明早|明晚|明日")
_DEICTIC_TODAY_RX = re.compile(r"今天|今早|今晚|待会|等会儿|等下")
_DIARY_RX = re.compile(r"^\s*20\d{2}\s*[-年/]")  # 日记体开头（语录里混入日记段落时防御）
# 指代时间词不能当独立记忆节点：含义随日期飘移，单独激活只会污染 G_active
DEICTIC_KEYWORDS = {"明天", "明早", "明晚", "明日", "今天", "今早", "今晚",
                    "昨晚", "昨天", "现在", "刚才", "待会", "等会儿", "等下"}

_DIM_CN = {
    "happy": "开心", "sad": "难过", "angry": "生气",
    "fear": "害怕", "love": "依恋", "surprise": "惊讶",
}


def _extract_keywords(text: str, limit: int) -> List[str]:
    words: List[str] = []
    if _HAS_JIEBA:
        words = jieba.lcut(text)
    else:  # pragma: no cover
        words = [text[i:i + 2] for i in range(0, len(text) - 1, 2)]
    out: List[str] = []
    for w in words:
        w = w.strip()
        if len(w) < 2 or w in STOPWORDS:
            continue
        if w.isdigit() or _PRUNE_RX.fullmatch(w):
            continue
        if re.fullmatch(r"[a-zA-Z]{1,2}", w):
            continue
        if w not in out:
            out.append(w)
        if len(out) >= max(1, limit):
            break
    return out


class MemoryGraph:
    """类型化有向图 G=(V, E, M, E_v)。所有时间单位：小时。"""

    def __init__(self, path: str, cfg: Optional[Dict[str, Any]] = None):
        self.path = path
        cfg = cfg or {}
        self.edge_init = clamp(float(cfg.get("edge_init", 0.25)), 0.01, 1.0)
        self.w_max = clamp(float(cfg.get("w_max", 2.5)), 0.1, 5.0)
        self.theta_act = clamp(float(cfg.get("theta_act", 0.30)), 0.0, 1.0)
        self.gamma = clamp(float(cfg.get("gamma_hebbian", 0.06)), 0.0, 1.0)
        self.alpha = clamp(float(cfg.get("alpha_coupling", 3.0)), 0.0, 10.0)
        self.recall_k = int(clamp(float(cfg.get("recall_k", 8)), 1, 20))
        self.spread_hops = int(clamp(float(cfg.get("spread_hops", 2)), 1, 3))
        self.max_keywords = int(clamp(float(cfg.get("max_keywords_per_msg", 8)), 1, 12))
        self.max_nodes = int(clamp(float(cfg.get("max_nodes", 4000)), 100, 50000))
        self.max_edges = int(clamp(float(cfg.get("max_edges", 12000)), 100, 200000))
        self.node_retention_days = float(clamp(
            float(cfg.get("node_retention_days", 180)), 1, 3650))

        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: Dict[Tuple[str, str], float] = {}
        self.used_anchors: Dict[str, float] = {}  # anchor_id → 使用时刻
        if os.path.exists(self.path):
            try:
                self._load()
            except Exception:
                self.nodes, self.edges, self.used_anchors = {}, {}, {}

    # ---------- 持久化 ----------

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for nid, nd in (data.get("nodes", {}) or {}).items():
            try:
                e_tags = nd.get("E", {}) or {}
                self.nodes[str(nid)] = {
                    "type": str(nd.get("type", "fact")),
                    "content": str(nd.get("content", ""))[:120],
                    "a": clamp(float(nd.get("a", 0.0)), 0.0, 1.0),
                    "E": {d: clamp(float(e_tags.get(d, 0.0)), 0.0, 1.0)
                          for d in EMOTION_DIMS},
                    "created": float(nd.get("created", time.time())),
                    "last_active": float(nd.get("last_active", time.time())),
                    "sender": str(nd.get("sender", "")),  # §9.1(c) 主体归属重启不丢
                }
            except (TypeError, ValueError):
                continue
        for row in (data.get("edges", []) or []):
            try:
                u, v, w = str(row[0]), str(row[1]), float(row[2])
                self.edges[(u, v)] = clamp(w, 0.0, self.w_max)
            except (TypeError, ValueError, IndexError):
                continue
        self.used_anchors = {str(k): float(v)
                             for k, v in (data.get("used_anchors", {}) or {}).items()}

    def save(self) -> None:
        data = {
            "nodes": self.nodes,
            "edges": [[u, v, w] for (u, v), w in self.edges.items()],
            "used_anchors": self.used_anchors,
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".",
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ---------- 写入（用户消息 → 节点/边） ----------

    def add_observation(
        self, text: str, s_ext: Dict[str, float], now: Optional[float] = None,
        sender: str = "owner",
        doc_id: str = "",
        de_dt: float = 0.0,
        peak_u: float = 0.0,
        status: str = "pending",
    ) -> List[str]:
        """把一条用户消息写进图谱：关键词事实节点 + 事件节点（±情绪/高光节点）。

        sender：消息主体（owner=主人 / other=群聊其他人 / bot=机器人自述）。
        §9.1(c) 主体相关校验的数据基础——别人的记忆不能成为对主人开口的由头。

        返回本次创建/激活的节点 id 列表（供 Hebbian 共激活）。
        """
        now = now if now is not None else time.time()
        text = (text or "").strip()
        if not text:
            return []
        touched: List[str] = []
        e_dom, e_val = max(
            ((d, float(s_ext.get(d, 0.0))) for d in EMOTION_DIMS),
            key=lambda kv: kv[1],
        )
        keywords = _extract_keywords(text, self.max_keywords)
        event_id = ""
        if len(text) >= 6:
            # 保留较长原文：召回时才能带回完整事实细节（艾宾浩斯体感的关键）
            event_id = self._upsert_event_node(
                "event", f"聊到：{text[:200]}", s_ext, now, sender,
                status=status, frozen_at=now, doc_id=doc_id,
                de_dt=de_dt, peak_u=peak_u,
            )
            touched.append(event_id)

        for kw in keywords:
            kid = self._upsert_fact_node(kw, s_ext, now, sender)
            touched.append(kid)
            if event_id:
                self._strengthen(kid, event_id, self.edge_init)
        # 关键词共现边：同一条消息里一起出现的概念天然相关（§7.1 例子）
        for i in range(len(keywords)):
            for j in range(i + 1, len(keywords)):
                self._strengthen(f"kw:{keywords[i]}", f"kw:{keywords[j]}",
                                 self.edge_init * 0.6)
        # 高情绪消息额外落一个 emotion 节点（衰减最慢、最能"染色"日后心情）
        if e_val >= 0.45:
            eid = self._upsert_event_node(
                "emotion",
                f"{_DIM_CN.get(e_dom, e_dom)}的时刻：{text[:160]}",
                s_ext, now, sender,
                status=status, frozen_at=now, doc_id=doc_id,
                de_dt=de_dt, peak_u=peak_u,
            )
            touched.append(eid)
            for kw in keywords:
                self._strengthen(f"kw:{kw}", eid, self.edge_init)
        # 高光/创伤节点：e_dom 很强才落（一生难忘级别）
        if e_val >= 0.60:
            hid = self._upsert_event_node(
                "highlight", f"高光：{text[:160]}", s_ext, now, sender,
                status=status, frozen_at=now, doc_id=doc_id,
                de_dt=de_dt, peak_u=peak_u,
            )
            touched.append(hid)
        self._cap_size()
        return touched

    def _upsert_node(self, nid: str, ntype: str, content: str,
                     s_ext: Dict[str, float], now: float,
                     sender: str = "") -> None:
        nd = self.nodes.get(nid)
        if nd is None:
            self.nodes[nid] = {
                "type": ntype,
                "content": content[:260],
                "a": 0.0,
                "E": {d: clamp(float(s_ext.get(d, 0.0)), 0.0, 1.0)
                      for d in EMOTION_DIMS},
                "created": now,
                "last_active": now,
                "sender": str(sender or ""),  # §9.1(c)：记忆主体归属
            }
        else:
            nd["last_active"] = now
            if sender and not nd.get("sender"):
                nd["sender"] = str(sender)
            # 情感标签滚动平均：旧印象为主，新刺激小幅修正
            for d in EMOTION_DIMS:
                nd["E"][d] = clamp(
                    0.7 * nd["E"][d] + 0.3 * float(s_ext.get(d, 0.0)), 0.0, 1.0)

    def _upsert_fact_node(self, keyword: str, s_ext: Dict[str, float],
                          now: float, sender: str = "") -> str:
        nid = f"kw:{keyword}"
        self._upsert_node(nid, "fact", keyword, s_ext, now, sender)
        return nid

    def _upsert_event_node(self, ntype: str, content: str,
                           s_ext: Dict[str, float], now: float,
                           sender: str = "",
                           status: str = "pending",
                           frozen_at: Optional[float] = None,
                           doc_id: str = "",
                           de_dt: float = 0.0,
                           peak_u: float = 0.0) -> str:
        nid = f"{ntype}:{int(now * 1000)}"
        self._upsert_node(nid, ntype, content, s_ext, now, sender)
        nd = self.nodes.get(nid)
        if nd is not None:
            # 记忆 v2 的双阶段字段：pending 时先冻结峰值，异步摘要后再 committed
            nd["status"] = status
            nd["frozen_at"] = int(frozen_at if frozen_at is not None else now)
            nd["doc_id"] = doc_id or None
            nd["de_dt"] = float(de_dt)
            nd["peak_u"] = float(peak_u)
        return nid

    def mark_event_committed(self, nid: str, content: str = "",
                             edge_kw: Optional[List[str]] = None,
                             edge_emotion: Optional[List[str]] = None,
                             emotion_tag: str = "") -> bool:
        """双阶段固结的阶段二：异步摘要写回，不改已冻结的 de_dt / peak_u。"""
        nd = self.nodes.get(nid)
        if nd is None:
            return False
        if content:
            nd["content"] = str(content)[:260]
        if edge_kw:
            nd["edge_kw"] = list(edge_kw)
        if edge_emotion:
            nd["edge_emotion"] = list(edge_emotion)
        if emotion_tag:
            nd["emotion_tag"] = str(emotion_tag)
        nd["status"] = "committed"
        nd["last_active"] = time.time()
        return True

    def _strengthen(self, u: str, v: str, delta: float) -> None:
        if u == v:
            return
        key = (u, v)
        self.edges[key] = clamp(self.edges.get(key, 0.0) + delta, 0.0, self.w_max)
        # 反向补一条弱边：概念关联本就双向（扩散需要 N(v) 有能量可达）
        rkey = (v, u)
        self.edges[rkey] = clamp(self.edges.get(rkey, 0.0) + delta * 0.5,
                                 0.0, self.w_max)

    def _cap_size(self) -> None:
        """超限时剪掉最不活跃/最旧的节点与最弱的边。"""
        if len(self.nodes) > self.max_nodes:
            deadline = time.time() - self.node_retention_days * 86400
            victims = [
                nid for nid, nd in self.nodes.items()
                if nd["a"] < 0.01 and nd["last_active"] < deadline
            ]
            if not victims:  # 都还很新：按激活值垫底剪
                victims = sorted(self.nodes,
                                 key=lambda n: self.nodes[n]["a"])[: len(self.nodes) - self.max_nodes]
            for nid in victims[: max(0, len(self.nodes) - self.max_nodes)]:
                self._drop_node(nid)
        if len(self.edges) > self.max_edges:
            ordered = sorted(self.edges.items(), key=lambda kv: kv[1])
            for (u, v), _ in ordered[: len(self.edges) - self.max_edges]:
                del self.edges[(u, v)]

    def _drop_node(self, nid: str) -> None:
        self.nodes.pop(nid, None)
        for key in [k for k in self.edges if k[0] == nid or k[1] == nid]:
            del self.edges[key]

    # ---------- 召回（§5.3 扩散 + §5.2 注入） ----------

    def activate_and_spread(
        self, text: str, now: Optional[float] = None,
    ) -> List[Tuple[str, float]]:
        """以当前消息关键词为种子，按 Eq 5.3 扩散 spread_hops 轮，返回 top-k [(nid, a)]。"""
        now = now if now is not None else time.time()
        seeds = [f"kw:{kw}" for kw in _extract_keywords(text, self.max_keywords)
                 if f"kw:{kw}" in self.nodes]
        if not seeds and self.nodes:
            # 没有命中关键词：给最近活跃的节点一点余温
            seeds = sorted(self.nodes,
                           key=lambda n: self.nodes[n]["last_active"],
                           reverse=True)[:1]
        act: Dict[str, float] = {nid: 1.0 for nid in seeds if nid in self.nodes}
        for nid in seeds:
            if nid in self.nodes:
                self.nodes[nid]["last_active"] = now
                self.nodes[nid]["a"] = clamp(
                    max(self.nodes[nid]["a"], 1.0), 0.0, 1.0)

        out_adj: Dict[str, List[Tuple[str, float]]] = {}
        deg: Dict[str, int] = {}
        for (u, v), w in self.edges.items():
            out_adj.setdefault(u, []).append((v, w))
            deg[u] = deg.get(u, 0) + 1

        for _ in range(self.spread_hops):
            delta: Dict[str, float] = {}
            for u, a_u in list(act.items()):
                d_u = deg.get(u, 0)
                if d_u == 0 or a_u < 0.05:
                    continue
                for v, w in out_adj.get(u, []):
                    if v in self.nodes:
                        delta[v] = delta.get(v, 0.0) + w * a_u / d_u
            for v, dv in delta.items():
                act[v] = clamp(act.get(v, 0.0) + dv, 0.0, 1.0)
        for nid, a in act.items():
            if nid in self.nodes:
                self.nodes[nid]["a"] = clamp(
                    max(self.nodes[nid]["a"], a), 0.0, 1.0)
        # 排序：激活值 × 近因加成（48h 内最多 +50%）× 事实节点加成——
        # 让"最近聊过的具体事件"排到联想词前面（艾宾浩斯体感的关键）
        now_t = now

        def _rank(nid: str) -> float:
            a = act.get(nid, 0.0)
            nd = self.nodes.get(nid, {})
            age_h = (now_t - float(nd.get("last_active", now_t))) / 3600.0
            recency = 1.0 + 0.5 * max(0.0, 1.0 - age_h / 48.0)
            fact_bonus = 1.15 if nd.get("type") in ("event", "emotion",
                                                    "highlight") else 1.0
            return a * recency * fact_bonus

        recalled = sorted(act.keys(), key=_rank, reverse=True)
        # 召回首先是“想起一件事”，不是“想起一个词”。
        # kw 节点是索引壳子，种子激活恒为 1.0，会把真正带原文的 event 挤到后面；
        # 这里把事件类节点整体前置，关键词只在还有名额时补位。
        event_rows = [
            nid for nid in recalled
            if self.nodes.get(nid, {}).get("type") in ("event", "emotion", "highlight")
        ]
        keyword_rows = [nid for nid in recalled if nid not in event_rows]
        recalled = event_rows + keyword_rows
        recalled = [(nid, act[nid]) for nid in recalled]
        return recalled[: self.recall_k]

    def recall_emotion_injection(
        self, recalled: List[Tuple[str, float]],
    ) -> Dict[str, float]:
        """§5.2：s_int,i = Σ a(v)·E_{v,i} —— 召回的高情感节点反过来染色当前心情。"""
        s_int = {d: 0.0 for d in EMOTION_DIMS}
        for nid, a in recalled:
            nd = self.nodes.get(nid)
            if not nd:
                continue
            for d in EMOTION_DIMS:
                s_int[d] += a * nd["E"].get(d, 0.0)
        return {d: clamp(v, 0.0, 1.0) for d, v in s_int.items()}

    # ---------- Hebbian（§5.5） ----------

    def hebbian(self, active_ids: List[str], now: Optional[float] = None) -> int:
        """一起放电的神经元连在一起：本轮共激活节点两两之间 M += γ。返回更新边数。

        两道门（防"记忆错构"）：
        1) 跨主体硬门：sender 不同直接不连（前摄/倒摄抑制的机制层面——
           鑫鑫的抱怨 vs 小满自己的话一旦连边，就会在她嘴里串成"我记错了"）；
        2) 时间邻近惩罚：同一主体、创建时间相差 24h 内、且这条边还没被反复
           共现确立过（共现 < NEAR_REPEAT 次）→ 这次只加 γ·NEAR_SCALE，
           不按正常强度连。碰巧在同一场对话里被一起提起的两件事（"点外卖"
           和"下雨了"）不该长成强关联；只有跨过更长时间窗、被多次共现的，
           才算真正的关联。"""
        now = now if now is not None else time.time()
        active = [nid for nid in dict.fromkeys(active_ids)
                  if nid in self.nodes and self.nodes[nid]["a"] > self.theta_act]
        near_window = 24 * 3600.0
        near_scale = 0.2      # 时间邻近时的边权倍率（γ·0.2）
        near_repeat = 3       # 同一对节点跨时间窗共现 ≥3 次 = 已确立，正常强度
        n = 0
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                u, v = active[i], active[j]
                if self._sender_of(u) != self._sender_of(v):
                    continue
                delta = self.gamma
                if abs(self._born_of(u) - self._born_of(v)) < near_window:
                    seen = self.edges.get((u, v), 0.0) / max(self.gamma, 1e-9)
                    if seen < near_repeat:
                        delta = self.gamma * near_scale
                self.edges[(u, v)] = clamp(
                    self.edges.get((u, v), 0.0) + delta, 0.0, self.w_max)
                self.edges[(v, u)] = clamp(
                    self.edges.get((v, u), 0.0) + delta, 0.0, self.w_max)
                n += 2
        return n

    def _sender_of(self, nid: str) -> str:
        """节点主体归属；历史数据缺字段时视为主人（owner）。"""
        nd = self.nodes.get(nid) or {}
        return str(nd.get("sender", "owner") or "owner")

    def _born_of(self, nid: str) -> float:
        """节点首次出现的时间（时间邻近判据用）；缺字段回落到 last_active。"""
        nd = self.nodes.get(nid) or {}
        try:
            return float(nd.get("created") or nd.get("last_active") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ---------- 衰减（§5.3 衰减项 + §5.4 情绪调制） ----------

    def decay_step(self, e_dom: float, dt_hours: float) -> None:
        """每 tick 对全部节点 a 乘 exp(-λ_v(e)·Δt)。λ_v(e) 由当前主导情绪调制。"""
        if dt_hours <= 0:
            return
        dt_hours = min(dt_hours, 6.0)  # 防休眠后一步跳变过大
        for nid, nd in self.nodes.items():
            lam_type = LAMBDA_TYPE.get(nd["type"], 0.1)
            e_v = max(nd["E"].values()) if nd["E"] else 0.0
            # §5.4：情绪调制。重要度不在此处调制——memory_v2 与 graph 各自生成
            # node_id，回写链路不通，恒为 +0；imp 的生效点在 memory_v2 的
            # 衰减（λ_eff）与检索排序（乘性项）。
            lam = lam_type / (1.0 + self.alpha * clamp(e_dom, 0.0, 1.0) * e_v)
            lam = clamp(lam, 0.0, 1.0)
            # 指数衰减（步长无关，§5.2 连续口径）；欧拉近似在 dt=6h 时误差显著
            nd["a"] = clamp(nd["a"] * math.exp(-lam * dt_hours), 0.0, 1.0)

    # ---------- 梦境固化（§7.3 夜间重放 + 修剪） ----------

    def dream(self, now: Optional[float] = None,
              prune_below: Optional[float] = None) -> Dict[str, int]:
        """对当天高激活节点重放（再点亮 + Hebbian 强化），修剪弱边与陈旧死节点。"""
        now = now if now is not None else time.time()
        day_start = now - 86400
        day_nodes = [
            nid for nid, nd in self.nodes.items()
            if nd["last_active"] >= day_start and nd["a"] > self.theta_act
        ]
        replayed = self.hebbian(day_nodes, now)  # 重放 = 共激活再走一遍
        prune_below = self.edge_init * 0.4 if prune_below is None else prune_below
        pruned_edges = 0
        for key in [k for k, w in self.edges.items() if w < prune_below]:
            del self.edges[key]
            pruned_edges += 1
        deadline = now - self.node_retention_days * 86400
        dead = [nid for nid, nd in self.nodes.items()
                if nd["a"] < 0.002 and nd["last_active"] < deadline]
        for nid in dead:
            self._drop_node(nid)
        return {"replayed_pairs": replayed, "pruned_edges": pruned_edges,
                "dropped_nodes": len(dead)}

    # ---------- 由头锚点（§9：一次性事由，同一由头一生只出现一次） ----------

    def anchor_candidates(self, now: Optional[float] = None,
                          sender_filter: str = "owner") -> List[Dict[str, str]]:
        """从已有数据派生一次性事由（§9.1 校验在源头执行）：
        - revisit  : 回访旧事 —— 高情感旧节点（highlight/emotion，隔天以上）
        - promise  : 承诺跟进 —— 内容里带承诺词的节点（隔 2 小时以上）
        同一节点一生只当一次锚点（used_anchors）。

        §9.1 可识别性校验（缺一不进候选）：
        (a) 节点绑定：候选必须绑定具体节点 ID + 内容摘要（本函数天然满足）
        (b) 时间明确：节点 created 时间戳可追溯（本函数天然满足）
        (c) 主体相关：sender_filter 非 "" 时，仅保留该主体的记忆节点
            （群聊里别人的事不能成为对主人开口的由头）
        """
        now = now if now is not None else time.time()
        out: List[Dict[str, str]] = []
        for nid, nd in self.nodes.items():
            if nid in self.used_anchors or not nd["content"]:
                continue
            if nd["type"] == "quote":  # 语录是风格语料，不作为开口由头
                continue
            if sender_filter and nd.get("sender", "owner") not in (
                    sender_filter, ""):  # 未知归属视为主人的（历史数据兼容）
                continue
            age_h = (now - nd["created"]) / 3600.0
            # 指代未来的时间词会过期："明天要考试"过完那天中午就不再是"明天"——
            # 过期锚点直接跳过，宁可不提也不说时序错乱的话（修复考试事故）
            if self._promise_expired(nd["content"], nd["created"], now):
                continue
            if nd["type"] in ("highlight", "emotion") and age_h >= 24:
                out.append({"kind": "revisit", "node": nid,
                            "content": nd["content"],
                            "created": round(nd["created"], 1),
                            "score": nd["a"] + nd["E"].get("love", 0.0)})
            elif (_PROMISE_RX.search(nd["content"])
                  and not _FAKE_PROMISE_RX.search(nd["content"])
                  and age_h >= 2):
                out.append({"kind": "promise", "node": nid,
                            "content": nd["content"],
                            "created": round(nd["created"], 1),
                            "score": 0.5 + nd["a"]})
        out.sort(key=lambda c: c["score"], reverse=True)
        return out[:6]

    @staticmethod
    def _promise_expired(content: str, created: float, now: float) -> bool:
        """指代未来的精确时间词过期判定：
        - '明天/明早'类：只在其次日中午前有效（次日中午一过，'明天'已是过去）
        - '今天/今晚'类：只在当日午夜前有效
        '下次/改天/回头/周末'等开放式承诺不过期（语义本身不含精确日期）。"""
        try:
            dt = datetime.datetime.fromtimestamp(created)
        except (OSError, OverflowError, ValueError):
            return False
        if _DEICTIC_TOMORROW_RX.search(content):
            expiry = dt.replace(hour=12, minute=0, second=0) + datetime.timedelta(days=1)
            return now > expiry.timestamp()
        if _DEICTIC_TODAY_RX.search(content):
            expiry = dt.replace(hour=23, minute=59, second=59)
            return now > expiry.timestamp()
        return False

    @staticmethod
    def validate_anchor(anchor: Dict[str, Any]) -> tuple:
        """§9.1 锚点可识别性三校验（发送前最后一道闸）。返回 (ok, reason)。
        只对"事由回访类"（node 非空应校验）执行；情绪表达类（node 为空）天然通过。"""
        node = str((anchor or {}).get("node", "") or "")
        if not node:
            return True, "情绪表达类，无需锚点"  # 表 9-2：思念/关心/情绪同步不受校验
        nd = (anchor or {}).get("_node_data") or {}
        # (a) 节点绑定
        if not str(nd.get("content", "") or ""):
            return False, "(a) 节点绑定缺失：锚点无内容摘要"
        # (b) 时间明确
        created = nd.get("created", 0)
        if not created or float(created) <= 0:
            return False, "(b) 时间不明确：节点无写入时间戳"
        # (c) 主体相关
        if str(nd.get("sender", "owner") or "owner") not in ("owner", ""):
            return False, "(c) 主体不符：该记忆属于其他主体，不能作为对主人开口的由头"
        return True, "校验通过"

    @staticmethod
    def when_label(ts: float, now: Optional[float] = None) -> str:
        """相对时间标签：让 LLM 分清记忆的过去式/现在式（修复'考完没'类时序割裂）。"""
        now = now if now is not None else time.time()
        h = max(0.0, (now - float(ts)) / 3600.0)
        if h < 1:
            return "刚刚聊过"
        if h < 24:
            return f"{int(h)}小时前聊过"
        d = int(h // 24)
        if d == 1:
            return "昨天聊过"
        if d <= 7:
            return f"{d}天前聊过"
        if d <= 30:
            return f"{d // 7}周前聊过"
        return f"{d // 30}个月前聊过"

    def recent_events(self, now: Optional[float] = None, hours: float = 24,
                      limit: int = 4) -> List[Dict[str, Any]]:
        """最近聊过的话题（event/emotion/highlight，带时间标签）——意图包的
        "最近你们聊过"数据源，让主动开口衔接当前对话、被动回复分清时序。"""
        now = now if now is not None else time.time()
        deadline = now - hours * 3600
        rows = [nd for nid, nd in self.nodes.items()
                if nd["type"] in ("event", "emotion", "highlight")
                and nd["created"] >= deadline and nd.get("content")
                and not nd["content"].startswith("反思：")]
        rows.sort(key=lambda n: -n["created"])
        return [{"content": nd["content"][:80],
                 "when": self.when_label(nd["created"], now),
                 "sender": str(nd.get("sender", "owner") or "owner"),
                 "a": round(nd["a"], 3)}
                for nd in rows[:limit]]

    # ---------- 语录（§18.3 表达层：她自己的话作 few-shot 风格示例） ----------

    def add_bot_event(self, text: str, s_ext: Optional[Dict[str, float]] = None,
                      now: Optional[float] = None) -> str:
        """她自己的行动/主动发言写入记忆（§7.5 R_t 的 bot 侧时序锚）。

        只建 event 节点（sender=bot），不拆关键词 fact——行动记录不是概念
        联想材料。让"我想"和"它做了"在记忆里接上：改签名/发说说/翻唱/
        主动消息都会出现在下一轮意图包的"最近聊过"里，
        消除"动的那一下我不在场"的解离叙事（11-08 观察到的架构断点）。
        """
        now = now if now is not None else time.time()
        s_ext = s_ext or {}
        content = (text or "").strip()[:200]
        if not content:
            return ""
        # 同内容去重（24h 内）：行动只发生一次，记忆也只能有一次——
        # 补录/重复回执不能制造"改了两次签名"的假记忆
        for nid, nd in self.nodes.items():
            if (nd["type"] == "event" and nd.get("sender") == "bot"
                    and nd["content"] == content
                    and now - float(nd.get("created", 0)) < 86400):
                # 只有补录时间贴近动作发生时刻（回执重复投递）才刷新激活；
                # 补录历史动作（相差 >1h）保持原时序，when 标签不失真
                if abs(now - float(nd.get("created", 0))) < 3600:
                    nd["last_active"] = now
                return nid
        eid = self._upsert_event_node("event", content, s_ext, now, sender="bot")
        self._cap_size()
        return eid

    def add_quote(self, text: str, s_ext: Optional[Dict[str, float]] = None,
                  now: Optional[float] = None) -> str:
        """存一条她自己说过的话（type=quote，sender=bot）。

        quote 是表达风格语料，不是经历记忆——被 g_active / anchor_candidates /
        recent_events 排除，不会污染"聊过什么"的时序；只供 bot_quotes 取用。
        """
        now = now if now is not None else time.time()
        s_ext = s_ext or {}
        nid = f"quote:{int(now * 1000)}"
        self._upsert_node(nid, "quote", (text or "").strip()[:200],
                          s_ext, now, sender="bot")
        return nid

    def bot_quotes(self, limit: int = 4, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """最近的自我语录（带当时心情标签）——意图包"说话示例"数据源（§18.3）。"""
        now = now if now is not None else time.time()
        rows = [nd for nd in self.nodes.values()
                if nd["type"] == "quote" and nd.get("content")]
        rows.sort(key=lambda n: -n["created"])
        out = []
        for nd in rows[:limit * 2]:  # 多取一倍候选，过滤日记体后仍够 limit 条
            txt = nd["content"]
            # 语录只收聊天体短句：日记段落（日期开头/超长）会让 few-shot 把
            # 聊天腔带成"日记腔"，甚至让她用第一人称复述架构机制（11-08 事故）
            if _DIARY_RX.match(txt) or len(txt) > 90:
                continue
            e = nd.get("E", {}) or {}
            dom = max(e, key=lambda k: float(e.get(k, 0.0))) if e else ""
            mood = _DIM_CN.get(dom, dom) if float(e.get(dom, 0.0)) >= 0.15 else "平静"
            out.append({"content": txt[:160],
                        "mood": mood,
                        "when": self.when_label(nd["created"], now)})
            if len(out) >= limit:
                break
        return out

    def mark_anchor_used(self, node_id: str, now: Optional[float] = None) -> None:
        self.used_anchors[node_id] = float(now if now is not None else time.time())

    # ---------- 观测 ----------

    def top_active(self, n: int = 5) -> List[Tuple[str, str, float]]:
        rows = sorted(self.nodes.items(),
                      key=lambda kv: kv[1]["a"], reverse=True)[:n]
        return [(nid, nd["content"], nd["a"]) for nid, nd in rows]
