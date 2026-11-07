"""MindEngine：把 e/a/θ/ψ/u/g 串成闭环（论文 §5.10 四个耦合回路）。

    回路A  e → s_int → a → E_v → e   情绪染色记忆召回，召回反过来染色情绪
    回路B  a → M(u,v) → a            共激活增强边权，边权影响后续扩散
    回路C  e,a → u → 行为 → 反馈 → θ → λ/阈值 → e,a   完整闭环
    回路D  θ → λ_v → a → s_int → e   慢参数控制快动力学

纯 Python：状态与图谱落盘 JSON，tick 由宿主（main.py 心跳任务）驱动。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .adapt import AdaptEngine
from .classify import estimate_emotion
from .emotion import EmotionField, LAMBDA_TYPE
from .goals import goal_drift
from .graph import MemoryGraph
from .state import DEFAULT_PSI, DEFAULT_THETA, EMOTION_DIMS, MindState, clamp
from .urge import UrgeEngine

logger = logging.getLogger("shihai.core")

_DIM_CN = {
    "happy": "开心", "sad": "难过", "angry": "生气",
    "fear": "害怕", "love": "依恋", "surprise": "惊讶",
}

# 全部可调参数（调参接口的校验面）：key → (min, max, is_int, 说明)
PARAM_META: Dict[str, Tuple[float, float, bool, str]] = {
    # —— 情绪场（§5.1/§6）——
    "tau_happy":     (0.5, 48, False, "开心弛豫时间 h"),
    "tau_sad":       (0.5, 48, False, "难过弛豫时间 h"),
    "tau_angry":     (0.5, 48, False, "生气弛豫时间 h"),
    "tau_fear":      (0.5, 48, False, "害怕弛豫时间 h（§8 再 ×(1+N)）"),
    "tau_love":      (0.5, 72, False, "依恋弛豫时间 h（默认24，最长）"),
    "tau_surprise":  (0.5, 48, False, "惊讶弛豫时间 h"),
    "kappa":         (0.0, 3.0, False, "刺激增益 κ：外部/内部刺激对情绪的强度"),
    "eta_relax":     (0.0, 1.0, False, "情绪自相关 η：互激励惯性"),
    # —— 图谱（§5.3/5.4/5.5、§7.2）——
    "lambda_fact":     (0.0, 1.0, False, "事实节点基础衰减率/h"),
    "lambda_emotion":  (0.0, 1.0, False, "情绪节点基础衰减率/h"),
    "lambda_event":    (0.0, 1.0, False, "事件节点基础衰减率/h"),
    "lambda_highlight": (0.0, 1.0, False, "高光节点基础衰减率/h"),
    "alpha_coupling":  (0.0, 10.0, False, "情绪-衰减耦合强度 α（论文3.0）"),
    "gamma_hebbian":   (0.0, 1.0, False, "Hebbian 学习率 γ"),
    "theta_act":       (0.0, 1.0, False, "激活阈值 θ_act"),
    "edge_init":       (0.01, 1.0, False, "新边初始权重"),
    "w_max":           (0.1, 5.0, False, "边权上限"),
    "recall_k":        (1, 20, True, "扩散召回 top-k"),
    "spread_hops":     (1, 3, True, "扩散跳数"),
    "max_keywords_per_msg": (1, 12, True, "每条消息最多提取关键词数"),
    "prune_edge_below": (0.0, 1.0, False, "梦境修剪：低于此权重的边被剪"),
    "node_retention_days": (1, 3650, True, "死节点保留天数"),
    "dream_hour":      (0, 23, True, "梦境固化时刻（点）"),
    # —— 念头（§5.6/§9）——
    "base_rate":        (0.0, 3.0, False, "念头基础增速 du/dt 基线"),
    "rhythm_on":        (0.0, 3.0, False, "作息命中时段增速（论文1.45）"),
    "rhythm_off":       (0.0, 3.0, False, "作息未命中时段增速（论文0.48）"),
    "quiet_start":      (0, 23, True, "安静时段开始（点）"),
    "quiet_end":        (0, 23, True, "安静时段结束（点）"),
    "urge_ceiling":     (1.0, 10.0, False, "念头上限（论文3.0）"),
    "streak_gate_step": (0.0, 1.0, False, "被冷落一次门槛抬升量（论文0.2）"),
    "fire_threshold":   (0.5, 3.0, False, "开口阈值基准 FIRE_THRESHOLD（论文2.10）"),
    "tantrum_threshold": (0.1, 2.0, False, "发脾气阈值基准（§8 实际×(1+A)）"),
    "send_threshold":   (0.0, 3.0, False, "依恋 att：回复一致性调制主动频率"),
    "eff":              (0.0, 1.0, False, "自我效能（成功+0.05/失败-0.1）"),
    # —— 自适应（§5.7/§5.8/§11）——
    "eta_adapt":        (0.0, 1.0, False, "自适应学习率 η_adapt"),
    "threshold_max":    (1.5, 6.0, False, "有效阈值硬饱和上限（自洽报告3.5）"),
    "threshold_floor":  (0.5, 3.0, False, "阈值下限"),
    "silence_hours":    (0.5, 24.0, False, "主动消息无回复多少小时算冷场（论文2h）"),
    "silence_step":     (0.0, 1.0, False, "冷场后阈值抬升步长"),
    "reengage_minutes": (1, 120, True, "几秒内接续算热络（论文5分钟）"),
    "reengage_step":    (0.0, 0.5, False, "接续后阈值下调步长"),
    "suppress_hours":   (0.5, 48.0, False, "『别老找我』后压制主动的时长"),
    "care_after_hours": (0.5, 168.0, False, "多久没聊后'关心近况'成为合法由头（小时）"),
    "q_gain":           (0.0, 0.5, False, "Q 值步长（L_adapt）"),
    "eff_gain":         (0.0, 0.3, False, "正反馈自我效能增量"),
    "eff_drop":         (0.0, 0.5, False, "负反馈自我效能减量"),
    # —— 人格 ψ（§8）——
    "O": (0.0, 1.0, False, "开放性 O"),
    "C": (0.0, 1.0, False, "尽责性 C"),
    "E": (0.0, 1.0, False, "外向性 E：调制主动开口概率"),
    "A": (0.0, 1.0, False, "宜人性 A：调制发脾气阈值"),
    "N": (0.0, 1.0, False, "神经质 N：调制恐惧弛豫时间"),
    # —— 目标 g（§10）——（β 目标-策略耦合属行为插件 shihai_behavior）
    "g_relation": (0.0, 1.0, False, "目标：维持亲密关系"),
    "g_memory":   (0.0, 1.0, False, "目标：整理巩固记忆"),
    "g_self":     (0.0, 1.0, False, "目标：自我认知成长"),
    "g_avoid":    (0.0, 1.0, False, "目标：避免冲突被讨厌"),
}


class MindEngine:
    def __init__(self, state_path: str, graph_path: str, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.state = MindState(state_path)
        self.graph = MemoryGraph(graph_path, self._graph_cfg())
        self.emotion = EmotionField(
            tau={d: float(cfg.get(f"tau_{d}", v))
                 for d, v in (("happy", 6.0), ("sad", 12.0), ("angry", 2.0),
                              ("fear", 3.0), ("love", 24.0), ("surprise", 1.0))},
            kappa=float(cfg.get("kappa", 2.5)),
            eta=float(cfg.get("eta_relax", 0.15)),
            w_matrix=self._w_from_cfg(cfg),
            psi=self.state.psi,
        )
        self.urge = UrgeEngine(
            base_rate=float(cfg.get("base_rate", 0.55)),
            rhythm_on=float(cfg.get("rhythm_on", 1.45)),
            rhythm_off=float(cfg.get("rhythm_off", 0.48)),
            quiet_start=int(cfg.get("quiet_start", 1)),
            quiet_end=int(cfg.get("quiet_end", 8)),
            urge_ceiling=float(cfg.get("urge_ceiling", 3.0)),
            psi_ref=self.state.psi,
        )
        self.adapt = AdaptEngine(
            eta_adapt=float(cfg.get("eta_adapt", 0.6)),
            threshold_floor=float(cfg.get("threshold_floor", 1.2)),
            threshold_max=float(cfg.get("threshold_max", 3.5)),
            silence_hours=float(cfg.get("silence_hours", 2.0)),
            silence_step=float(cfg.get("silence_step", 0.10)),
            reengage_minutes=float(cfg.get("reengage_minutes", 5)),
            reengage_step=float(cfg.get("reengage_step", 0.05)),
            streak_gate_step=float(cfg.get("streak_gate_step", 0.2)),
        )
        self.care_after_hours = clamp(float(cfg.get("care_after_hours", 12)), 0.1, 168.0)
        self._last_tick_at: float = time.time()
        self._last_recall: List[Tuple[str, float]] = []
        self._last_self_expression_at: float = 0.0
        self._last_negative_at: float = 0.0
        # 心理插件（shihai_psyche）扩展的反馈词典（sync_psyche 时刷新）
        self._extra_lexicon: Dict[str, List[str]] = {"positive": [], "negative": []}
        # θ/ψ 初值如果配置里给了，覆盖默认
        for key in ("fire_threshold", "tantrum_threshold", "send_threshold", "eff"):
            if key in cfg:
                try:
                    self.state.theta[key] = float(cfg[key])
                except (TypeError, ValueError):
                    pass
        for key in DEFAULT_PSI:
            if key in cfg:
                try:
                    self.state.psi[key] = clamp(float(cfg[key]), 0.0, 1.0)
                except (TypeError, ValueError):
                    pass

    # ---------- 配置映射 ----------

    def _graph_cfg(self) -> Dict[str, Any]:
        return {
            "edge_init": self.cfg.get("edge_init", 0.25),
            "w_max": self.cfg.get("w_max", 2.5),
            "theta_act": self.cfg.get("theta_act", 0.30),
            "gamma_hebbian": self.cfg.get("gamma_hebbian", 0.06),
            "alpha_coupling": self.cfg.get("alpha_coupling", 3.0),
            "recall_k": self.cfg.get("recall_k", 8),
            "spread_hops": self.cfg.get("spread_hops", 2),
            "max_keywords_per_msg": self.cfg.get("max_keywords_per_msg", 8),
            "prune_edge_below": self.cfg.get("prune_edge_below", 0.10),
            "node_retention_days": self.cfg.get("node_retention_days", 180),
        }

    @staticmethod
    def _w_from_cfg(cfg: Dict[str, Any]) -> Optional[Dict[str, Dict[str, float]]]:
        """配置里的 w_matrix 是 6×6 互激励矩阵的 JSON 字符串；空/坏值回退内置默认。"""
        import json as _json
        raw = str(cfg.get("w_matrix", "") or "")
        if not raw:
            return None
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return None

    # ---------- 输入：用户消息（回路C 的入口） ----------

    def observe_user_text(self, text: str, sender: str = "", now: float = 0.0,
                          feedback: bool = True) -> Dict[str, Any]:
        """一条用户消息进来：s_ext 注入 → 反馈检测 → 图谱写入 → 扩散召回 → s_int → 情绪子步。

        feedback=False（非主人消息且开启 owner_only_feedback）时只做感知与记忆，
        不动 θ/Q —— 论文 §11 的适配只对主人生效。
        """
        now = now or time.time()
        text = (text or "").strip()[:500]
        s_ext = estimate_emotion(text)
        feedback_events = self.adapt.on_user_message(
            self.state, text, now,
            extra_positive=self._extra_lexicon.get("positive"),
            extra_negative=self._extra_lexicon.get("negative"),
        ) if feedback else []
        if feedback:
            for ev in feedback_events:
                if ev.get("type") in ("strong_negative", "short_reply"):
                    self._last_negative_at = now
        touched = self.graph.add_observation(text, s_ext, now)
        self.graph.hebbian(touched, now)
        recalled = self.graph.activate_and_spread(text, now)
        self._last_recall = recalled
        s_int = self.graph.recall_emotion_injection(recalled)
        # 每条消息按"持续刺激 12 分钟"积分（论文弛豫测试即为小时级持续刺激；
        # 单条消息若只积分消息间隙的几秒，情绪场永远抬不起来）
        self.state.e = self.emotion.step(self.state.e, s_ext, s_int, 0.2)
        self.state.last_user_msg_at = now
        self.state.save()
        self.graph.save()
        return {
            "s_ext": s_ext,
            "s_int": s_int,
            "feedback": feedback_events,
            "recall": [(nid, round(a, 3)) for nid, a in recalled[:5]],
            "e": {d: round(v, 3) for d, v in self.state.e.items()},
        }

    # ---------- 心跳 tick（回路A/D 的推进器） ----------

    def tick(self, now: float = 0.0, proactive_enabled: bool = False,
             psyche_path: str = "") -> None:
        """每 tick_seconds 调一次。念头攒满时写 state.fire_pending，
        由行为插件（shihai_behavior）取走并执行——核心不决定说什么。"""
        now = now or time.time()
        dt_h = clamp((now - self._last_tick_at) / 3600.0, 0.0, 6.0)
        self._last_tick_at = now
        if dt_h <= 0:
            return
        # 心理插件（shihai_psyche）对 ψ/效能/依恋的单向同步（psyche → core）
        if psyche_path:
            self.sync_psyche(psyche_path)
        # s_int 持续存在：最近召回的记忆仍在激活，持续染色情绪（回路A）
        s_int = self.graph.recall_emotion_injection(self._last_recall)
        self.state.e = self.emotion.step(self.state.e, {}, s_int, dt_h)
        # 图谱衰减（回路D：e_dom 经 α 调制 λ_v）
        e_dom, e_val = EmotionField.dominant(self.state.e)
        self.graph.decay_step(e_val, dt_h)
        # 念头积累（§5.6）
        recent_max_a = max((a for _, a in self._last_recall), default=0.0)
        suppressed = now < self.state.suppress_until
        hour = time.localtime(now).tm_hour
        self.state.urge = self.urge.step(
            self.state.urge, self.state.e, recent_max_a, hour, dt_h, suppressed)
        # 目标漂移（§10）
        interaction_recent = (now - self.state.last_user_msg_at) < 2 * 3600
        negative_recent = (now - self._last_negative_at) < 6 * 3600
        self_expr_age = (now - self._last_self_expression_at) / 3600.0
        goal_drift(self.state, dt_h, hour, interaction_recent,
                   negative_recent, self_expr_age)
        # 静默惩罚（§11 第3行）
        self.adapt.on_silence(self.state, now)
        # 开火判定（§5.6 + 饱和上限）：写 fire_pending，行为插件消费
        if (proactive_enabled and not suppressed and not self.state.fire_pending
                and self.urge.should_fire(
                    self.state.urge,
                    self.state.theta["fire_threshold"],
                    self.state.streak_gate,
                    self.adapt.threshold_max)):
            self._emit_fire_pending(now)
        self.state.save()
        self.graph.save()

    # ---------- Behavior API（论文 §13.1：核心发抽象指令，适配器执行） ----------

    def _emit_fire_pending(self, now: float) -> None:
        """念头攒满 → 把"该说话了"+由头锚点候选写进状态，等行为插件取走。
        核心不做策略选择（π 属行为层），只提供动力学事实。"""
        candidates = self.graph.anchor_candidates(now)
        long_silence_h = (now - self.state.last_user_msg_at) / 3600.0
        if long_silence_h >= self.care_after_hours:  # 有一阵没聊：关心近况是合法由头
            candidates.append({"kind": "recent", "node": "", "content": "",
                               "score": 0.55})
        if not candidates:
            return  # 没有任何由头：念头继续攒，等有了事由再发（不发空指令）
        self.state.fire_pending = {
            "emitted_at": now,
            "anchors": [
                {"kind": c["kind"], "node": c.get("node", ""),
                 "content": c.get("content", ""), "score": round(c.get("score", 0.0), 3)}
                for c in candidates[:6]
            ],
            "urge": round(self.state.urge, 3),
        }
        logger.info("[识海] 念头攒满（u=%.2f），已发 fire_pending（%d 个锚点候选）",
                    self.state.urge, len(candidates))

    def consume_fired(self, sig: Dict[str, Any]) -> bool:
        """行为插件发完消息后的回执：清 pending、念头清零、锚点一生一次、记账待验证行为。"""
        if not self.state.fire_pending:
            return False
        try:
            if float(sig.get("emitted_at", 0)) < float(
                    self.state.fire_pending.get("emitted_at", 0)):
                return False  # 过期回执，忽略
        except (TypeError, ValueError):
            pass
        action = str(sig.get("action", ""))
        text = str(sig.get("text", ""))
        node = str(sig.get("anchor_node", "") or "")
        now = float(sig.get("ts", 0) or time.time())
        self.state.fire_pending = {}
        self.state.urge = 0.0
        self.state.last_bot_proactive_at = now
        self.state.last_proactive_text = text[:120]
        self.state.last_proactive_action = action
        self.state.last_proactive_replied = False
        if node and node in self.graph.nodes:
            self.graph.mark_anchor_used(node, now)
        self.state.save()
        self.graph.save()
        return True

    # ---------- 梦境固化（§7.3，g_memory 驱动） ----------

    def dream(self, now: float = 0.0) -> Dict[str, Any]:
        now = now or time.time()
        result = self.graph.dream(now, prune_below=float(
            self.cfg.get("prune_edge_below", 0.10)))
        self.state.goals["g_memory"] = clamp(
            self.state.goals["g_memory"] - 0.3, 0.0, 1.0)  # 目标被满足 → 回落
        self.state.last_dream_date = time.strftime("%Y-%m-%d", time.localtime(now))
        self.state.save()
        self.graph.save()
        return result

    # ---------- 原始观测（意图包的组装属行为插件 shihai_behavior） ----------

    def g_active(self, anchor_node: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        """当前高激活记忆节点（附记忆 ID）——意图包 I_t 的 G_active 字段数据源。"""
        out: List[Dict[str, Any]] = []
        seen = set()
        if anchor_node and anchor_node in self.graph.nodes:
            nd = self.graph.nodes[anchor_node]
            if nd.get("content"):
                out.append({"id": anchor_node, "content": nd["content"][:160],
                            "a": round(nd["a"], 3)})
                seen.add(anchor_node)
        for nid, a in self._last_recall:
            if nid in seen or nid not in self.graph.nodes:
                continue
            nd = self.graph.nodes[nid]
            if nd.get("content"):
                seen.add(nid)
                out.append({"id": nid, "content": nd["content"][:160],
                            "a": round(a, 3)})
        for nid, _c, a in self.graph.top_active(6):
            if nid in seen or a < self.graph.theta_act:
                continue
            nd = self.graph.nodes.get(nid, {})
            if nd.get("content"):
                seen.add(nid)
                out.append({"id": nid, "content": nd["content"][:160],
                            "a": round(a, 3)})
        return out[:limit]

    def sync_psyche(self, psyche_path: str) -> None:
        """单向同步心理插件（shihai_psyche）的 psyche_state.json → z(t)。

        心理插件拥有 ψ/效能/依恋的取值权；核心每个 tick 读一次并应用。
        文件缺失或损坏时静默跳过（心理插件未安装也完全不影响核心）。
        """
        try:
            with open(psyche_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        psi = data.get("psi", {})
        if isinstance(psi, dict):
            for key in DEFAULT_PSI:
                try:
                    v = float(psi.get(key, self.state.psi[key]))
                    self.state.psi[key] = clamp(v, 0.0, 1.0)
                except (TypeError, ValueError):
                    pass
        theta = data.get("theta", {})
        if isinstance(theta, dict):
            for key in ("eff", "send_threshold", "tantrum_threshold"):
                if key in theta:
                    try:
                        self.state.theta[key] = clamp(float(theta[key]), 0.0, 3.0)
                    except (TypeError, ValueError):
                        pass
        lex = data.get("extra_lexicon", {})
        if isinstance(lex, dict):
            for key in ("positive", "negative"):
                words = lex.get(key, [])
                if isinstance(words, list):
                    self._extra_lexicon[key] = [str(w) for w in words if str(w).strip()]

    # ---------- 调参接口（参数面：WebUI 配置 + shihai_tune 工具共用） ----------

    def set_param(self, key: str, value: float) -> float:
        """校验并应用一个参数，返回钳位后的值。只处理数值参数；非法 key 抛 KeyError。"""
        if key not in PARAM_META:
            raise KeyError(key)
        lo, hi, is_int, _ = PARAM_META[key]
        val = float(value)
        val = clamp(val, lo, hi)
        if is_int:
            val = int(round(val))
        self.cfg[key] = val
        self._apply_param(key, val)
        return float(val)

    def _apply_param(self, key: str, val: float) -> None:
        # 情绪场
        if key.startswith("tau_"):
            dim = key.split("_", 1)[1]
            if dim in self.emotion.tau:
                self.emotion.tau[dim] = max(0.1, val)
            return
        if key == "kappa":
            self.emotion.kappa = val
        elif key == "eta_relax":
            self.emotion.eta = val
        # 图谱
        elif key == "alpha_coupling":
            self.graph.alpha = val
        elif key == "gamma_hebbian":
            self.graph.gamma = val
        elif key == "theta_act":
            self.graph.theta_act = val
        elif key == "edge_init":
            self.graph.edge_init = val
        elif key == "w_max":
            self.graph.w_max = val
        elif key == "recall_k":
            self.graph.recall_k = int(val)
        elif key == "spread_hops":
            self.graph.spread_hops = int(val)
        elif key == "max_keywords_per_msg":
            self.graph.max_keywords = int(val)
        elif key == "node_retention_days":
            self.graph.node_retention_days = val
        elif key.startswith("lambda_"):
            LAMBDA_TYPE[key.split("_", 1)[1]] = val
        # 念头
        elif key == "base_rate":
            self.urge.base_rate = val
        elif key == "rhythm_on":
            self.urge.rhythm_on = val
        elif key == "rhythm_off":
            self.urge.rhythm_off = val
        elif key == "quiet_start":
            self.urge.quiet_start = int(val)
        elif key == "quiet_end":
            self.urge.quiet_end = int(val)
        elif key == "urge_ceiling":
            self.urge.urge_ceiling = val
        elif key == "streak_gate_step":
            self.adapt.streak_gate_step = val
        # θ
        elif key in ("fire_threshold", "tantrum_threshold", "send_threshold", "eff"):
            self.state.theta[key] = val
        # 自适应
        elif key == "eta_adapt":
            self.adapt.eta_adapt = val
        elif key == "threshold_max":
            self.adapt.threshold_max = val
        elif key == "threshold_floor":
            self.adapt.threshold_floor = val
        elif key == "silence_hours":
            self.adapt.silence_hours = val
        elif key == "silence_step":
            self.adapt.silence_step = val
        elif key == "reengage_minutes":
            self.adapt.reengage_minutes = val
        elif key == "reengage_step":
            self.adapt.reengage_step = val
        elif key == "suppress_hours":
            self.adapt.suppress_hours = val
        elif key == "care_after_hours":
            self.care_after_hours = val
        elif key == "q_gain":
            self.adapt.q_gain = val
        elif key == "eff_gain":
            self.adapt.eff_gain = val
        elif key == "eff_drop":
            self.adapt.eff_drop = val
        # 人格
        elif key in DEFAULT_PSI:
            self.state.psi[key] = val  # emotion.psi 引用同一 dict，自动生效
        # 目标
        elif key.startswith("g_"):
            self.state.goals[key] = val

    # ---------- 观测 ----------

    def snapshot(self) -> Dict[str, Any]:
        snap = self.state.snapshot()
        snap["threshold_effective"] = round(
            self.adapt.threshold_effective(self.state), 3)
        snap["g_active"] = self.g_active()
        snap["graph"] = {
            "nodes": len(self.graph.nodes),
            "edges": len(self.graph.edges),
            "anchors_used": len(self.graph.used_anchors),
            "top_active": [
                {"id": nid, "type": self.graph.nodes[nid]["type"],
                 "content": self.graph.nodes[nid]["content"][:160],
                 "a": round(a, 3)}
                for nid, _c, a in self.graph.top_active(5)
            ],
        }
        return snap
