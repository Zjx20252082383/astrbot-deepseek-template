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
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .adapt import AdaptEngine
from .classify import estimate_emotion
from .ego import EgoRegulator, RHO
from .emotion import EmotionField, LAMBDA_TYPE
from .goals import goal_drift
from .graph import DEICTIC_KEYWORDS, MemoryGraph, _extract_keywords
from .relationship import RelationshipMap
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
    # —— 自我调节 EgoRegulator（整合稿 §5.10）——
    "ego_enabled":       (0, 1, True, "自我调节总开关：1=发送须过 r_release>ρ 许可（§5.10a）"),
    "ego_rho":           (0.1, 0.9, False, "释放判定阈值 ρ（论文0.5，严格大于）"),
    "threshold_low_ratio": (0.3, 0.8, False, "低位拦截线比例：FIRE_THRESHOLD_LOW=比例×FIRE_THRESHOLD（论文0.5）"),
    "delta_delay":       (0.0, 2.0, False, "r_delay 折算系数 δ：冲突/冷落期判定阈值抬升量（§5.10b）"),
    "reflux_enabled":    (0, 1, True, "受控自我回流（§17 扩展模式，默认关；与默认配置互斥，开启须注明）"),
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
        # 关系子系统（§10.6）：与图谱同目录落盘，按人分开维护关系强度
        rel_path = os.path.join(os.path.dirname(graph_path) or ".", "relations.json")
        self.relations = RelationshipMap(rel_path)
        self._last_sender: str = ""
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
        # EgoRegulator（整合稿 §5.10 自我调节 + §17 受控回流）
        self.ego = EgoRegulator(
            rho=float(cfg.get("ego_rho", RHO)),
            low_ratio=float(cfg.get("threshold_low_ratio", 0.5)),
            reflux_enabled=bool(int(cfg.get("reflux_enabled", 0))),
        )
        self.ego_enabled = bool(int(cfg.get("ego_enabled", 1)))
        self.delta_delay = clamp(float(cfg.get("delta_delay", 1.0)), 0.0, 2.0)
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
                          feedback: bool = True, is_owner: bool = True) -> Dict[str, Any]:
        """一条用户消息进来：s_ext 注入 → 反馈检测 → 图谱写入 → 扩散召回 → s_int → 情绪子步。

        feedback=False（非主人消息且开启 owner_only_feedback）时只做感知与记忆，
        不动 θ/Q —— 论文 §11 的适配只对主人生效。
        is_owner：消息主体归属（§9.1(c) 主体相关校验的数据基础，写入图谱节点）。
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
        touched = self.graph.add_observation(
            text, s_ext, now,
            sender="owner" if is_owner else (f"other:{sender}" if sender else "other"))
        self.graph.hebbian(touched, now)
        # 关系边推进（§10.6）：按发送者分开记，广度=话题、深度=自述
        self._last_sender = "owner" if is_owner else (sender or "")
        self.relations.observe(
            self._last_sender, text, _extract_keywords(text, 4), now,
            float(self.state.psi.get("A", 0.7)))
        self.relations.save()
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
        # 关系衰减（§10.6：30 天半衰期，与 ψ 同为月级慢变量）
        self.relations.decay_all(now)
        goal_drift(self.state, dt_h, hour, interaction_recent,
                   negative_recent, self_expr_age,
                   relation_strength=self.relations.strength(self._last_sender))
        # 静默惩罚（§11 第3行）
        self.adapt.on_silence(self.state, now)
        # 冲突窗口衰减（§5.10.2(b)：conflict_steps 每决策步 -1）
        if self.state.conflict_steps > 0:
            self.state.conflict_steps -= 1
        # 开火判定（整合稿 §5.10a：u > 有效阈值(含 r_delay 抬升) ∧ r_release > ρ）
        if proactive_enabled and not suppressed and not self.state.fire_pending:
            over = self.urge.should_fire(
                self.state.urge,
                self.state.theta["fire_threshold"],
                self.state.streak_gate,
                min(float(self.adapt.threshold_max),
                    float(self.urge.urge_ceiling) - 0.01))
            if over:
                # r_delay 抬升判定阈值（§5.10b：FIRE ← FIRE + δ·r_delay）
                # 有效阈值永远不能顶到念头天花板：urge_ceiling=3.0 而
                # threshold_max=3.5 时，u 被钳在 3.0 就永远跨不过 3.5，
                # 会形成"越攒越沉默"的死锁。留 0.01 的余量保证高位可开口。
                ceiling = float(self.urge.urge_ceiling)
                eff_thr = min(
                    self.adapt.threshold_effective(self.state)
                    + self.delta_delay * self._pending_r_delay(),
                    ceiling - 0.01)
                over = self.state.urge > eff_thr  # 严格大于（论文口径）
            if over:
                r_top1 = self._last_recall[0][1] if self._last_recall else 0.0
                decision = self.ego.decide(
                    u=self.state.urge,
                    fire_threshold=self.state.theta["fire_threshold"],
                    urge_ceiling=self.urge.urge_ceiling,
                    e=self.state.e, psi=self.state.psi, goals=self.state.goals,
                    conflict_flag=self.state.conflict_steps > 0,
                    streak=self.state.streak_count,
                    r_feedback=self.state.r_feedback,
                    r_top1=r_top1,
                )
                if self.ego_enabled and decision.r_release <= self.ego.rho:
                    # 自我不许可：displacement → 替代行为（内部消化，绝不对外发送）；
                    # 其余防御 + §17 回流（可选）→ 记审计轨迹。u 不清零（§5.10 转移规则）
                    self._ego_intervene(decision, r_top1, now)
                else:
                    self._emit_fire_pending(now)
        self.state.save()
        self.graph.save()

    def _pending_r_delay(self) -> float:
        """r_delay 生成规则（§5.10.1 末）：冲突或 streak≥2 → 0.15，否则 0。"""
        return 0.15 if (self.state.conflict_steps > 0
                        or self.state.streak_count >= 2) else 0.0

    def _ego_intervene(self, decision, r_top1: float, now: float) -> None:
        """自我否决后的落点（整合稿 §5.10 转移规则 + §17 受控回流）。

        - displacement：替代行为 = 反思节点写入图谱（p₄，零 LLM，不对外输出，表 17-3）
        - u 不钳位不清零：念头保留在累积路径上，等 URGE_CEILING 高位分支放行
        - reflux_enabled 时按看门狗规则调一个快原语（p₁/p₂/p₃），全部写 ego_trace
        """
        entries = []
        if decision.defense == "displacement":
            # p₄ 图谱写入（慢，小时级）：把这次冲动固化为反思记忆
            rid = self.graph.add_observation(
                f"反思：有点想找主人说话，但此刻{decision.branch}",
                {d: 0.0 for d in EMOTION_DIMS}, now, sender="bot")[0:1]
            entries.append(EgoRegulator.trace_entry(now, "p4", rid[0] if rid else "", 1.0))
        if self.ego.reflux_enabled:
            primitive, new_e, new_u, delta = self.ego.pick_reflux(
                self.state.urge, self.state.e,
                self.state.theta["fire_threshold"])
            if primitive:
                if primitive in ("p1", "p3"):
                    self.state.e = new_e
                if primitive in ("p2", "p3"):
                    self.state.urge = new_u
                entries.append(EgoRegulator.trace_entry(
                    now, primitive, "e/u", delta))
        if decision.trace:
            entries.append({"ts": round(now, 3), "ego": decision.trace,
                            "r_top1": round(r_top1, 3)})
        self.state.ego_trace = self.ego.audit(
            (self.state.ego_trace or []) + entries)
        logger.info("[识海] 自我否决（%s）；u=%.2f 保留累积%s",
                    decision.defense, self.state.urge,
                    "，回流已执行" if self.ego.reflux_enabled else "")

    # ---------- Behavior API（论文 §13.1：核心发抽象指令，适配器执行） ----------

    _ANCHOR_REASON = {
        "revisit": "旧事在我心里又浮上来了",
        "promise": "答应过对方的事到了该回访的时候",
        "recent": "它有一阵没和我说话了",
        "will": "此刻就是很想开口，没有什么具体的事由",
    }

    # 已完成的语汇：出现即说明该事由已经了结，不能再拿它当开口理由
    _SETTLED_RX = re.compile(r"(已经|考完|交了|做完|去过了|见过了|结束了|搞定了|办完了)")
    # 这些字组成的双字片段不承载"同一件事"的信息，参与比较只会制造误杀
    _STOP_CHARS = set("了的是我你他她它在有就也都很不没和与这那什么")

    @classmethod
    def _bigrams(cls, text: str) -> set[str]:
        """取干净的中文双字片段。

        原实现用 `range(0, 20)` 直接切片，短句必然切出空串；任何含"了"
        的句子都会共享 `""` 与 `"了"` 两个二元组，于是"惊讶的时刻"被
        "我已经说过了"误判成同一件事而删除——由头被清空，fire_pending
        永远写不进去。这里先去掉标点，再剔除空串和停用字片段。
        """
        raw = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))
        if len(raw) < 2:
            return set()
        grams = {raw[i:i + 2] for i in range(len(raw) - 1)}
        return {g for g in grams if not any(ch in cls._STOP_CHARS for ch in g)}

    @classmethod
    def _drop_settled(cls, graph: "MemoryGraph", candidates: List[Dict[str, Any]],
                      now: float) -> List[Dict[str, Any]]:
        """剔除已被后续事实了结的由头（一致性守护 I1 的写入侧延伸）。

        旧事本身没错，但"明天考试"这类事由若已被更晚的"已经考完了"覆盖，
        再拿它当开口理由就会重现 F1（记忆错乱）。判据只用词表与时间，不调用模型。
        与 graph 层的指代时间词过期闸（_promise_expired）互补：
        一个管"语义上被新事实推翻"，一个管"指代词本身过期"。
        """
        keep: List[Dict[str, Any]] = []
        nodes = getattr(graph, "nodes", {}) or {}
        for c in candidates:
            node_id = str(c.get("node", "") or "")
            nd = nodes.get(node_id)
            if not nd:
                keep.append(c)
                continue
            created = float(nd.get("created", 0.0))
            settled = False
            for other_id, other in nodes.items():
                if other_id == node_id or float(other.get("created", 0.0)) <= created:
                    continue
                content = str(other.get("content", ""))
                if not cls._SETTLED_RX.search(content):
                    continue
                # 共享至少 2 个有信息量的双字片段才认为是同一件事
                a = cls._bigrams(nd.get("content", ""))
                b = cls._bigrams(content)
                if len(a & b) >= 2:
                    settled = True
                    break
            if not settled:
                keep.append(c)
        return keep

    @classmethod
    def _anchor_reason(cls, kind: str, content: str = "") -> str:
        """把锚点类型翻译成第一人称理由（纯查表，不调用模型）。"""
        base = cls._ANCHOR_REASON.get(str(kind or ""), "想起一件事")
        text = str(content or "").strip()
        return f"{base}（{text[:24]}）" if text else base

    def _emit_fire_pending(self, now: float) -> None:
        """念头攒满且自我许可 → 把"该说话了"+由头锚点候选写进状态，等行为插件取走。
        核心不做策略选择（π 属行为层），只提供动力学事实。
        §9.1：候选在 anchor_candidates 源头已过三校验（节点绑定/时间明确/主体相关）。"""
        candidates = self.graph.anchor_candidates(now)
        # 每个候选附带节点元数据，供行为插件生成时引用原文细节（修复上下文割裂）
        for c in candidates:
            nd = self.graph.nodes.get(c.get("node", ""))
            if nd:
                c["_node_data"] = {"content": nd.get("content", ""),
                                   "created": nd.get("created", 0),
                                   "sender": nd.get("sender", "owner")}
        long_silence_h = (now - self.state.last_user_msg_at) / 3600.0
        if long_silence_h >= self.care_after_hours:  # 有一阵没聊：关心近况是合法由头
            candidates.append({"kind": "recent", "node": "", "content": "",
                               "score": 0.55})
        candidates = self._drop_settled(self.graph, candidates, now)
        low = not candidates or max(
            float(c.get("score", 0.0)) for c in candidates) < 0.4
        if low:
            # 没有像样的外部由头（或候选只剩两天前一个很淡的旧影），
            # 但念头本身已经越过阈值：允许"凭意愿"开口。
            # 这不是空指令——u 就是这时候最真实的理由，行为层会据此选择
            # 开口/说说/画画/唱歌，而不是把攒满的念头静默丢掉。
            candidates = [{"kind": "will", "node": "", "content": "",
                           "score": 0.6}]
        # 最新事实优先（I1 时序单调）：先按节点写入时刻倒序，再按 score 兜底
        def _recency(c: Dict[str, Any]) -> float:
            nd = self.graph.nodes.get(str(c.get("node", "") or ""), {})
            return float(nd.get("created", 0.0))
        candidates.sort(key=lambda c: (-_recency(c), -float(c.get("score", 0.0))))
        top = candidates[0]
        reason = self._anchor_reason(top.get("kind", ""), top.get("content", ""))
        self.state.fire_pending = {
            "emitted_at": now,
            "intent_id": f"intent:{int(now * 1000)}",
            "reason": reason,
            "self_view": (
                f"我在 {time.strftime('%H:%M', time.localtime(now))} 决定主动向主人靠近，"
                f"因为{reason}；念头强度 u={self.state.urge:.2f}，"
                f"已越过有效阈值"
            ),
            "recent_talks": self.graph.recent_events(now),  # 最近聊过（时序锚）
            "anchors": [
                {"kind": c["kind"], "node": c.get("node", ""),
                 "content": c.get("content", ""),
                 "when": self.graph.when_label(
                     self.graph.nodes[c["node"]]["created"], now)
                 if c.get("node") in self.graph.nodes else "",
                 "created": c.get("created", 0),
                 "sender": c.get("_node_data", {}).get("sender", ""),
                 "score": round(c.get("score", 0.0), 3)}
                for c in candidates[:6]
            ],
            "urge": round(self.state.urge, 3),
        }
        logger.info("[识海] 念头攒满（u=%.2f），自我许可（%.2f>ρ），已发 fire_pending（%d 个锚点候选，事由：%s）",
                    self.state.urge, 1.0, len(candidates), reason[:30])

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
        # 真正送达 = 回执没被标记 skip 且 ok 不为 False。不能按"有没有 text"判：
        # 画图/唱歌成功的回执正文在 media 里，text 可能是空的。
        sent = bool(sig.get("ok", True)) and not bool(sig.get("skip", False))
        pending = dict(self.state.fire_pending or {})
        self.state.fire_pending = {}
        # A：只有真的发出去了才把念头清零。失败/跳过（音源拿不到、锚点校验未过）
        # 只压到有效阈值的 95%——否则一次外部故障就把攒到 u=2.1 的冲动抹成 0，
        # 得重攒 6 小时（实测爬升速率仅 0.00297/min），表现就是"意愿值骤降且起不来"。
        # 留 95% 意味着约半小时后自然重试，配合出口熔断换一个能发的出口去发。
        if sent:
            self.state.urge = 0.0
        else:
            self.state.urge = min(
                float(self.state.urge),
                float(self.adapt.threshold_effective(self.state)) * 0.95)
            # C：失败的这次也要扣 Q。否则被历史成功抬高的出口（如 sing_cover
            # Q=1.26 vs chat 0.36）会一直霸占选择权，每次都撞同一个死胡同。
            if action:
                self.adapt.q_update(self.state, action, False, 0.5)
        self.state.last_bot_proactive_at = now
        self.state.last_proactive_text = text[:120]
        # 只有送达的 action 才写进档案：失败的动作不能被主人后续的回复
        # 当成"这次主动奏效"记正反馈（配合 adapt 里去掉的兜底 action）。
        self.state.last_proactive_action = action if sent else ""
        self.state.last_proactive_replied = False
        # 决定档案：把"我想—我做了"写在同一处，供后续自我叙事引用
        # （"这是你自己的决定"——被动对话的她引用它来保持在场感）
        self.state.last_decision = {
            "intent_id": str(pending.get("intent_id", "")),
            "reason": str(pending.get("reason", "")),
            "self_view": str(pending.get("self_view", "")),
            "action": action,
            "text": text[:160],
            "song_reason": str(sig.get("reason", "") or "")[:80],
            "u": round(float(pending.get("urge", 0.0) or 0.0), 3),
            "ts": now,
        }
        if node and node in self.graph.nodes:
            self.graph.mark_anchor_used(node, now)
        if text:
            # §18.3 语料积累：她发出去的话存为语录（风格 few-shot 源，不进记忆时序）
            self.graph.add_quote(text, self.state.e, now)
            # §7.5 bot 侧时序锚：主动行动同时入记忆（进 R_t）——
            # 被动对话的她也要"在场"，知道刚才是自己做了这件事。
            # 说说已经由 _toolbox_record 写进 action_log（避免重复入账），
            # 其余出口按动作类型写正确的主语，不能一律说成"对主人说"。
            if action and action != "post_feed":
                if action == "sing_cover" and text.startswith("我"):
                    # 唱歌出口的文本本身就是"我唱了《X》给你听"，再套前缀会重复
                    self.graph.add_bot_event(text[:90], self.state.e, now)
                else:
                    prefix = {
                        "draw_share": "我画了一张图发给主人：",
                        "sing_cover": "我唱了一首歌给主人听：",
                    }.get(action, "我对主人说：")
                    self.graph.add_bot_event(f"{prefix}{text[:90]}",
                                             self.state.e, now)
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
        """当前高激活记忆节点（附记忆 ID + 相对时间标签）——意图包 I_t 的 G_active 字段数据源。
        when 标签让 LLM 分清记忆的过去式/现在式（修复"考完没"类时序割裂）。"""
        now = time.time()
        out: List[Dict[str, Any]] = []
        seen = set()
        if anchor_node and anchor_node in self.graph.nodes:
            nd = self.graph.nodes[anchor_node]
            if nd.get("content"):
                out.append({"id": anchor_node, "content": nd["content"][:160],
                            "when": self.graph.when_label(nd["created"], now),
                            "sender": str(nd.get("sender", "owner") or "owner"),
                            "a": round(nd["a"], 3)})
                seen.add(anchor_node)
        for nid, a in self._last_recall:
            if nid in seen or nid not in self.graph.nodes:
                continue
            nd = self.graph.nodes[nid]
            # 指代时间词（明天/现在…）单独成节点只会污染意图包时序，全出口拦截
            if nd.get("type") == "fact" and nd.get("content") in DEICTIC_KEYWORDS:
                continue
            if nd.get("content"):
                seen.add(nid)
                out.append({"id": nid, "content": nd["content"][:160],
                            "when": self.graph.when_label(nd["created"], now),
                            "sender": str(nd.get("sender", "owner") or "owner"),
                            "a": round(a, 3)})
        for nid, _c, a in self.graph.top_active(6):
            if nid in seen or a < self.graph.theta_act:
                continue
            nd = self.graph.nodes.get(nid, {})
            if nd.get("type") == "quote":  # 语录不是经历记忆，不进 G_active
                continue
            if nd.get("type") == "fact" and nd.get("content") in DEICTIC_KEYWORDS:
                continue
            if nd.get("content"):
                seen.add(nid)
                out.append({"id": nid, "content": nd["content"][:160],
                            "when": self.graph.when_label(nd["created"], now),
                            "sender": str(nd.get("sender", "owner") or "owner"),
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
        # EgoRegulator（整合稿 §5.10/§17）
        elif key == "ego_enabled":
            self.ego_enabled = bool(int(val))
        elif key == "ego_rho":
            self.ego.rho = val
        elif key == "threshold_low_ratio":
            self.ego.low_ratio = val
        elif key == "delta_delay":
            self.delta_delay = val
        elif key == "reflux_enabled":
            self.ego.reflux_enabled = bool(int(val))
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
        snap["recent_talks"] = self.graph.recent_events()  # 最近聊过（带时间标签）
        snap["relations"] = self.relations.snapshot()      # §10.6 按人分开的关系强度
        snap["bot_quotes"] = self.graph.bot_quotes()  # 自我语录（§18.3 说话示例）
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
