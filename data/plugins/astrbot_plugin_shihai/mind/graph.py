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

import json
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
    "的了是在我你他她它我们你们他们这那个有和就跟不很也都就还被把让给对说要去会能可以"
    "吗呢吧啊呀哦嗯噢喔么呐啦呗哈就是然后但是所以因为如果虽然不过还是已经正在一个一下"
    "没有是不是好了行吧现在什么时候怎么为什么这样那样自己东西事情感觉觉得知道看看出来"
    "起来过来下去上来回去回去有些一点非常真的直接今天明天昨天现在刚才等下等等而且或者除了"
    "时间剩下注意记得截点还是其中其中之后以前以后上次刚刚其实应该可能需要这样那样发现觉"
    "得开始最后问题地方工作内容消息状态参数配置主动回复请求适合有趣一样黄脸语音重新只是"
    "还有而是对话聊天记录系统框架模型提示工具调用功能插件服务账号主人日志测试"
    "the a an is are was were be to of and in on it this that i you he she we they "
    "for with not have do does did so but at as or if".split()
)

_PROMISE_RX = re.compile(r"(答应|承诺|明天|下次|改天|回头|周末|约定|记得|到时候|说了要)")
_PRUNE_RX = re.compile(r"[\s\d\W_]+$")

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
    ) -> List[str]:
        """把一条用户消息写进图谱：关键词事实节点 + 事件节点（±情绪/高光节点）。

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
                "event", f"聊到：{text[:200]}", s_ext, now)
            touched.append(event_id)

        for kw in keywords:
            kid = self._upsert_fact_node(kw, s_ext, now)
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
                s_ext, now,
            )
            touched.append(eid)
            for kw in keywords:
                self._strengthen(f"kw:{kw}", eid, self.edge_init)
        # 高光/创伤节点：e_dom 很强才落（一生难忘级别）
        if e_val >= 0.60:
            hid = self._upsert_event_node(
                "highlight", f"高光：{text[:160]}", s_ext, now)
            touched.append(hid)
        self._cap_size()
        return touched

    def _upsert_node(self, nid: str, ntype: str, content: str,
                     s_ext: Dict[str, float], now: float) -> None:
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
            }
        else:
            nd["last_active"] = now
            # 情感标签滚动平均：旧印象为主，新刺激小幅修正
            for d in EMOTION_DIMS:
                nd["E"][d] = clamp(
                    0.7 * nd["E"][d] + 0.3 * float(s_ext.get(d, 0.0)), 0.0, 1.0)

    def _upsert_fact_node(self, keyword: str, s_ext: Dict[str, float],
                          now: float) -> str:
        nid = f"kw:{keyword}"
        self._upsert_node(nid, "fact", keyword, s_ext, now)
        return nid

    def _upsert_event_node(self, ntype: str, content: str,
                           s_ext: Dict[str, float], now: float) -> str:
        nid = f"{ntype}:{int(now * 1000)}"
        self._upsert_node(nid, ntype, content, s_ext, now)
        return nid

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
        """一起放电的神经元连在一起：本轮共激活节点两两之间 M += γ。返回更新边数。"""
        now = now if now is not None else time.time()
        active = [nid for nid in dict.fromkeys(active_ids)
                  if nid in self.nodes and self.nodes[nid]["a"] > self.theta_act]
        n = 0
        for i in range(len(active)):
            for j in range(i + 1, len(active)):
                u, v = active[i], active[j]
                self.edges[(u, v)] = clamp(
                    self.edges.get((u, v), 0.0) + self.gamma, 0.0, self.w_max)
                self.edges[(v, u)] = clamp(
                    self.edges.get((v, u), 0.0) + self.gamma, 0.0, self.w_max)
                n += 2
        return n

    # ---------- 衰减（§5.3 衰减项 + §5.4 情绪调制） ----------

    def decay_step(self, e_dom: float, dt_hours: float) -> None:
        """每 tick 对全部节点 a 乘 (1 - λ_v(e)·dt)。λ_v(e) 由当前主导情绪调制。"""
        if dt_hours <= 0:
            return
        dt_hours = min(dt_hours, 6.0)  # 防休眠后一步跳变过大
        for nid, nd in self.nodes.items():
            lam_type = LAMBDA_TYPE.get(nd["type"], 0.1)
            e_v = max(nd["E"].values()) if nd["E"] else 0.0
            lam = lam_type / (1.0 + self.alpha * clamp(e_dom, 0.0, 1.0) * e_v)
            lam = clamp(lam, 0.0, 1.0)
            nd["a"] = clamp(nd["a"] * (1.0 - min(0.9, lam * dt_hours)), 0.0, 1.0)

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

    def anchor_candidates(self, now: Optional[float] = None) -> List[Dict[str, str]]:
        """从已有数据派生一次性事由：
        - revisit  : 回访旧事 —— 高情感旧节点（highlight/emotion，隔天以上）
        - promise  : 承诺跟进 —— 内容里带承诺词的节点（隔 2 小时以上）
        同一节点一生只当一次锚点（used_anchors）。
        """
        now = now if now is not None else time.time()
        out: List[Dict[str, str]] = []
        for nid, nd in self.nodes.items():
            if nid in self.used_anchors or not nd["content"]:
                continue
            age_h = (now - nd["created"]) / 3600.0
            if nd["type"] in ("highlight", "emotion") and age_h >= 24:
                out.append({"kind": "revisit", "node": nid,
                            "content": nd["content"],
                            "score": nd["a"] + nd["E"].get("love", 0.0)})
            elif _PROMISE_RX.search(nd["content"]) and age_h >= 2:
                out.append({"kind": "promise", "node": nid,
                            "content": nd["content"],
                            "score": 0.5 + nd["a"]})
        out.sort(key=lambda c: c["score"], reverse=True)
        return out[:6]

    def mark_anchor_used(self, node_id: str, now: Optional[float] = None) -> None:
        self.used_anchors[node_id] = float(now if now is not None else time.time())

    # ---------- 观测 ----------

    def top_active(self, n: int = 5) -> List[Tuple[str, str, float]]:
        rows = sorted(self.nodes.items(),
                      key=lambda kv: kv[1]["a"], reverse=True)[:n]
        return [(nid, nd["content"], nd["a"]) for nid, nd in rows]
