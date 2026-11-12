"""识海记忆运行时（v2 链路，唯一记忆链路）。

旧链路（消息入队 → 攒批 LLM 抽取 → memory.json → 按 (weight, ts) 注入）已删除。
现在全部走 ``memory_v2.MemoryV2`` 三层记忆：

- 工作记忆：AstrBot 会话上下文（不归本模块管）
- 事实层 fact.json：稳定身份/关系/约定，慢衰减；来自运行时 LLM 固结 + 迁移
- 情景层 event_snapshot.json：带情绪峰值的事件摘要；冻结挂在心智 ODE 峰值上

链路：
  写（被动）：memory_observe 把用户消息存进滚动缓冲（原文快照）
              → 行为层看门狗每轮调 memory_watch(state)
              → 检测情绪跳变（|de/dt| 尖峰）或念头峰值（urge ≥ 2.0）
              → MemoryV2.on_ode_peak 同步冻结 pending 事件
              → 异步任务调一次 LLM：事件摘要 + 事实三元组 → commit_pending + add_fact
  读（被动）：memory_inject 在每次 LLM 请求前 recall(用户消息) → prompt 拼装
  命令/Web：/记忆 /记住 /忘掉 /清空记忆 + suite Web API，全部直写 v2

峰值量纲说明：EPS_DE_DT=0.30 的单位是 h⁻¹（论文连续动力学口径）。核心插件
observe_user_text 每条消息按 12 分钟持续刺激积分（emotion.step(e, s_ext, s_int, 0.2)），
单条消息的 Δe ≈ 0.01~0.05，落在 20s 看门狗窗口里折算 de/dt ≈ 2~9 h⁻¹；
而无刺激的连续衰减只有 |de/dt| = e/τ ≤ 0.5 h⁻¹。因此这里用可配置尖峰阈值
``de_dt_spike``（默认 2.0 h⁻¹，≈ 单窗 Δe≥0.011）来判定"情绪显著跳变"，
比论文常数 0.30 更严——按 0.30 执行会把每条非中性消息都冻结成事件。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from .base import SuiteBase, to_int
from .query_classifier import needs_timeline
from .memory_v2 import (
    EVENT_CONTENT_MAX, EVENT_TOP_N, FACT_TOP_N, TIMELINE_DAYS, MemoryV2, grams,
    is_degraded_content, is_raw_transcript, owner_label)
from .transcript_store import TranscriptStore
from .relational import (RelationStore, quota, valence_from_text,
                         visible_weight)
from .selfstate import SessionStore, session_block
from .modes import mode_for, hint as mode_hint_of, MODE_COMPANION, MODE_RESTRICTED
from .commitments import CommitmentLedger, extract as extract_commit
from .roster import Roster

# 数据目录沿用旧契约（与 memory_v2.py 自检路径一致）
NAME = "astrbot_plugin_qq_toolbox"
# 单条事实注入的字数上限：存量 v1 事实会有 80~110 字的长句，一条吃光预算
FACT_LINE_MAX = 60

# 称呼口径：对方叫「鑫鑫」，她自己叫「小满」。记忆与转写里不使用「主人」「宠物」这类称呼。
OWNER_NAME = "鑫鑫"

# 归属可见性（v5）：非主人会话要不要读 owner 层（鑫鑫的私有记忆）。
#   True  = 老行为：一个库全共享，靠"注入归属声明 + 输出号码护栏"兜底，
#           好处是 D 组在新号上也有记忆可答，Day -1 的锚点直接生效。
#   False = 严格隔离：外人只看自己那一层（更干净，但新号开局空白，
#           D 组会答"不知道"），切之前必须先把 Day -1 锚点写到 shared 层。
OWNER_LAYER_VISIBLE_TO_OTHERS = True
SELF_NAME = "小满"

# 时间线三档：(天数上界, 系数, 单条字数上限, 档名)。三档互不重叠——
# 越近的档系数越高、字数越宽、给的名额越多；越远的档越压缩，因为"最近的
# 那几天"才是她会主动提起的。天数上界之差构成各档的区间：0~2 / 3~6 / 7~14 天。
TIMELINE_TIERS = (
    (3, 1.00, 36, "近三天"),
    (7, 0.85, 30, "近一周"),
    (15, 0.70, 24, "近半月"),
)
# 兜底单条上限（命中不了任何档时用）
TIMELINE_LINE_MAX = TIMELINE_TIERS[0][2]

_COMMIT_PROMPT = """你是记忆整理助手。下面是你（小满，住在 QQ 里的蓝色大肥鱼）和鑫鑫刚刚的一段对话记录。
称呼只用「鑫鑫」和「小满」；不要写「主人」「宠物」「饲主」这类称呼，转写里也别用。
请输出 JSON：
{"event": "用不超过100字概括这段对话里发生的事，写清楚时间点和结果",
 "emotion_tag": "happy/sad/angry/fear/love/surprise/calm 里选一个最贴近的",
 "imp": 0.0到1.0（一位小数，凭你自己的感觉：这件事在你心里有多重）,
 "facts": [{"p": "谓词短语（≤10字，如：喜欢吃/养的猫叫/约好/在准备/讨厌）",
            "o": "宾语短语（≤24字）", "imp": 0.0到1.0（一位小数）}]}
facts 最多 3 条，只记稳定的事（身份、喜好、约定、近况），没有就给 []。
imp 判定口径（防止什么都重要）：≥0.85 万一忘了会真的出事（约定/考试/生病/生日/身份）；
0.6-0.8 值得主动想起来（明确计划、情绪事件、他反复提过的偏好）；0.4-0.5 日常交流；
≤0.3 闲聊寒暄。只输出 JSON，不要解释。"""

# 重要度规则兜底：仅在 LLM 未给出 imp（超时/解析失败）时使用，不做 max
_RULE_IMP_STRONG_RX = re.compile(
    r"(约定|答应|承诺|生日|生病|吃药|考试|面试|还钱|借钱|身份证|全名|我叫)")
_RULE_IMP_PLAN_RX = re.compile(
    r"(明天|今晚|晚上|周末|下周|约好|记得|别忘了|一起|准备|要去)")

_MEM_PROMISE_RX = re.compile(r"(约好|答应|说好|承诺|约定|生日|下次|等你|回来)")

HELP_TEXT = """记忆相关指令：
/记忆  看看我记得关于你的事
/记住 <内容>  手动记一件事（核心记忆，不衰减）
/忘掉 <关键词>  删掉相关的一条事实
/清空记忆  清空自己的事实层（鑫鑫可加「全部」）
/记忆帮助  显示这段说明"""


class MemoryMixin(SuiteBase):
    # ==================== 初始化 ====================

    def setup_memory(self) -> None:
        # 数据在 plugin_data/<NAME>/memory_v2/ 下（fact.json / event_snapshot.json）
        base = Path(self.module_data_dir(NAME)) / "memory_v2"
        self._mem_base = Path(base)
        self._memv2 = MemoryV2(base)
        # 逐字归档库（方案 A）：本地 SQLite，与 JSON 摘要层并行
        self._transcript = TranscriptStore(Path(base) / "transcript.db")
        # z_r(o) 关系层 / z_s(s) 会话实例层（论文 6.9 三分解；仅 e_partitioned 影响行为）
        self._rel = RelationStore(Path(base) / "relation_state.json")
        self._sess = SessionStore(Path(base) / "session_state.json")
        # 双姿态路由（生活助理 / 情感陪伴）与承诺账簿：都按会话 tag 分片
        self._known_ids: set[str] = set()
        self._commit = CommitmentLedger(Path(base) / "commitments.json")
        # 熟人名单：她只能"提议"，改名单只能由鑫鑫一句话触发（见 roster.py 头注）
        self._roster = Roster(Path(base))
        try:  # 她自己的号 + 主人号永不成候选（否则她会提议把"她自己"记成熟人）
            self._roster.set_exclude([
                getattr(self, "bot_qid", ""), getattr(self, "owner_qid", ""),
                *self._roster.known()])
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[identity] 名单排除初始化失败: {type(exc).__name__} {exc}")
        # M1/M2 采集：每次注入的 w / R_o / 逐条 owner 落 jsonl（论文 6.12.4）
        self._mem_trace_path = Path(base) / "metrics_trace.jsonl"
        self._mem_commit_cache: dict[str, tuple[float, bool]] = {}
        # 滚动缓冲：最近的对话原文快照（峰值冻结时取材；重启丢失，不落盘）
        self._mem_buffer: list[dict[str, Any]] = []
        self._mem_last_content_ts: float = 0.0
        # 峰值检测状态
        self._mem_prev_e: dict[str, float] | None = None
        self._mem_prev_ts: float = 0.0
        self._mem_last_freeze: float = 0.0
        self._mem_last_bank_tick: float = 0.0
        # imp 滚动分布：窗口级通胀抑制 + 分布日志用；落盘 imp_hist.json 跨重启保留
        self._mem_imp_hist_path = Path(base) / "imp_hist.json"
        try:
            self._mem_imp_hist: list[float] = [
                float(v) for v in json.loads(
                    self._mem_imp_hist_path.read_text(encoding="utf-8"))
                if isinstance(v, (int, float))
            ][-100:]
        except Exception:  # noqa: BLE001 - 缺文件/坏 JSON 时从空开始
            self._mem_imp_hist = []
        self._mem_tasks: set[asyncio.Task] = set()
        # 固结解析失败计数 / provider 配置告警一次性标记
        self._mem_parse_fails = 0
        self._mem_provider_warned = False
        self._mem_provider_logged = False

    @filter.on_astrbot_loaded()
    async def _mem_on_loaded(self):
        """AstrBot 加载完成后打印记忆统计——setup_memory 执行早于日志 handler
        就绪，放这里才不会把「记忆链路就绪」吞掉。"""
        n_facts = len(self._memv2.facts.items)
        n_events = sum(
            1 for e in self._memv2.events.items.values() if e.status == "committed"
        )
        logger.info("[memoryv2] 记忆链路就绪：事实 %d 条 / 情景事件 %d 条", n_facts, n_events)
        # 关系层冷启动分档：事实库里出现过的 owner 视为熟人（R=0.70），避免把
        # "第一次出现但其实是熟人"按 0.15 误伤成陌生人（共享层名额只剩 2 条）。
        # 同时给存量事实补冲突簇键（只写标签，不动权重、不触发博弈）。
        try:
            known = {
                str(getattr(it, "owner", "owner"))
                for it in self._memv2.facts.items.values()
                if str(getattr(it, "owner", "owner")).startswith(("qq:", "group:"))
            }
            seeded = self._rel.register_known(known)
            pinned = self._rel.pin_owner()
            if pinned:
                self._rel.save()
            added = self._memv2.facts.backfill_conflict_groups()
            if added:
                self._memv2.facts.save()
            logger.info("[memoryv2] 熟人种子 %d 个（已知 %d），冲突簇键补 %d 条，owner R 钉值=%s",
                        seeded, len(known), added, pinned)
            # 熟人名单（权限层的第二档）：来源是"事实库里出现过的人"+"关系层已抬过 R 的人"
            # +配置里的手写名单。判定是离散的（在不在这个集合里），不是打分。
            ids: set[str] = set()
            for tag in known:
                if tag.startswith("qq:"):
                    ids.add(tag[3:])
            try:
                for obj in (self._rel.__dict__.get("rows") or {}):
                    s = str(obj)
                    if s.startswith("qq:"):
                        ids.add(s[3:])
            except Exception:  # noqa: BLE001
                pass
            try:
                raw = str(self._memory_cfg("known_friends", "") or "")
                for x in re.split(r"[,，;；\s]+", raw):
                    x = x.strip()
                    if x:
                        ids.add(x[3:] if x.startswith("qq:") else x)
            except Exception:  # noqa: BLE001
                pass
            # 独立文件优先：AstrBot 启动时会按插件 schema 重写配置文件，
            # schema 里没有的键（此处 known_friends）会被静默丢掉 —— 已实测。
            # 所以名单放在数据目录，不进配置 schema。
            try:
                kf = self._mem_base / "known_friends.json"
                if kf.exists():
                    raw_kf = json.loads(kf.read_text(encoding="utf-8-sig"))
                    seq = raw_kf if isinstance(raw_kf, list) else (
                        raw_kf.get("friends") if isinstance(raw_kf, dict) else [])
                    for x in seq or []:
                        s = str(x).strip()
                        if s:
                            ids.add(s[3:] if s.startswith("qq:") else s)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[identity] 熟人名单文件读取失败: {type(exc).__name__} {exc}")
            self._known_ids = ids
            logger.info("[identity] 熟人名单 %d 个：%s", len(ids),
                        ",".join(sorted(ids)[:6]) or "-")
        except Exception as exc:  # noqa: BLE001 - 初始化失败不阻断启动
            logger.warning(f"[memoryv2] 关系层/冲突簇初始化失败: {type(exc).__name__} {exc}")
        # 启动时就把"固结打分模型到底能不能解析到"说清楚：配了 id 但 inst_map 里
        # 没有时会静默回落会话默认模型，光看日志里的 imp 是看不出来的。
        try:
            pid = str(self._memory_cfg("extract_provider_id") or "").strip()
            pm = getattr(self.context, "provider_manager", None)
            inst = getattr(pm, "inst_map", None)
            keys = sorted(inst.keys()) if isinstance(inst, dict) else []
            if pid:
                hit = pid in keys
                logger.info("[memoryv2] 固结打分模型 %s：%s（现有 provider: %s）",
                            pid, "已注册" if hit else "未注册→会回落会话默认",
                            ", ".join(keys) or "无")
            else:
                logger.info("[memoryv2] 固结打分模型：未配置（回落会话默认，imp 可能全是一个值）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memoryv2] 固结模型自检失败: {exc}")
        # 实验组自检：切组后必须能在启动日志里看到，否则会以为切了实际没切。
        # a_none 组不注入记忆、不打注入日志，没有这一行就完全无法确认生效。
        try:
            raw_exp = None
            cfg = getattr(self, "config", None)
            if isinstance(cfg, dict):
                raw_exp = cfg.get("experiment")
            elif cfg is not None:
                raw_exp = getattr(cfg, "get", lambda *_: None)("experiment")
            exp = raw_exp if isinstance(raw_exp, dict) else {}
            logger.info(
                "[memoryv2][exp] 生效组=%s（experiment.enabled=%s，timeline_on_demand=%s）",
                self._experiment_group(), bool(exp.get("enabled", False)),
                self._timeline_on_demand())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memoryv2][exp] 组别自检失败: {exc}")

    def _memory_cfg(self, key: str, default: Any = None) -> Any:
        return self.cfg("memory", key, default)

    def _timeline_on_demand(self) -> bool:
        """时间线按需注入开关（默认关 = 保持原来的每轮都带）。

        放在 memory.basic 里，与 fact_top_n 同层，读取方式与它们一致。
        关掉时行为与改造前完全一致，回退只要把这一个开关改回 false。
        """
        try:
            return bool(self._memory_cfg("timeline_on_demand", False))
        except Exception:  # noqa: BLE001 - 读配置失败就退回旧行为
            return False

    # ==================== 消融实验分组（论文对照用） ====================

    # 四组递进设计，每组只比上一组多一个变量，便于写进论文：
    #   a_none             无记忆       —— 不注入任何记忆，纯人格 + 会话上下文
    #   b_recall           纯语义召回   —— 只注入相关事实/事件，排序按字面重合（等价普通 RAG）
    #   c_recall_timeline  召回 + 时序  —— 再加"这些天的片段"，补回日期维度
    #   d_full             完整识海     —— 排序再叠加激活 a / 重要度 imp / weight 动力学
    #   e_partitioned      自我状态分区 —— d_full + z_p/z_r(o)/z_s(s) 三分解、
    #                       共享层按 w_visible 连续门控、视角标记、时间线隔离
    # 默认 d_full：不配 experiment 时行为与改造前完全一致。
    EXPERIMENT_GROUPS = ("a_none", "b_recall", "c_recall_timeline", "d_full",
                         "e_partitioned")

    # 论文态默认：没有 experiment 配置（或未开启）时也跑自我状态分区。
    # 先把 6.9–6.11 的构想落成默认行为，不再依赖实验开关；要做消融时显式配
    # experiment.group 即可（a/b/c/d 四组保持原语义，仍然可用）。
    PRODUCTION_GROUP = "e_partitioned"

    def _experiment_group(self) -> str:
        """读实验组别；未开启实验或值非法时回落 PRODUCTION_GROUP（论文态）。

        直接取 config["experiment"] 而不是走 self.cfg：cfg 在目标分组不存在时
        会全表递归找同名键，可能命中 memory.basic.enabled 之类，把"没配实验"
        误判成"实验已开启"。
        """
        try:
            exp: dict[str, Any] = {}
            config = getattr(self, "config", None)
            if isinstance(config, dict):
                raw_exp = config.get("experiment")
                exp = raw_exp if isinstance(raw_exp, dict) else {}
            elif config is not None:
                raw_exp = getattr(config, "get", lambda *_: None)("experiment")
                exp = raw_exp if isinstance(raw_exp, dict) else {}
            else:
                return self.PRODUCTION_GROUP
            if not bool(exp.get("enabled", False)):
                return self.PRODUCTION_GROUP
            raw = str(exp.get("group", "d_full") or "d_full").strip()
            if raw not in self.EXPERIMENT_GROUPS:
                logger.warning(
                    "[exp] 未知组别 %r，回落 %s（可选：%s）",
                    raw, self.PRODUCTION_GROUP, " / ".join(self.EXPERIMENT_GROUPS))
                return self.PRODUCTION_GROUP
            label = str(exp.get("label", "") or "").strip()
            # 只在组别变化时打一次，避免每轮注入都刷同一条
            if getattr(self, "_exp_logged", None) != raw:
                self._exp_logged = raw
                logger.info("[exp:%s] 实验分组=%s %s", raw, raw,
                            label or "未标注批次")
            return raw
        except Exception as exc:  # noqa: BLE001 - 配置读失败不能打断对话
            logger.warning(f"[exp] 读取实验分组失败，按 {self.PRODUCTION_GROUP} 运行: {exc}")
            return self.PRODUCTION_GROUP

    # ==================== 写链路：观察消息 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def memory_observe(self, event: AstrMessageEvent):
        """把用户消息存进滚动缓冲，供情绪峰值冻结时取对话原文。"""
        if not bool(self._memory_cfg("enabled", True)):
            return
        try:
            sender = str(event.get_sender_id() or "")
            if not sender or sender == str(self.bot_qid):
                return
            text = self._memory_text(event)
            if not text or text.startswith("/"):
                return
            now = time.time()
            self._mem_buffer.append(
                {"ts": now, "sender": sender, "text": text[:120],
                 "origin": str(getattr(event, "unified_msg_origin", "") or "")})
            # 本轮归属层：冻结事件时写进 EventItem，注入时按会话取并集
            self._mem_last_owner = self._mem_owner_tag(
                sender, str(getattr(event, "unified_msg_origin", "") or ""))
            # z_r(o) / z_s(s) 推进（每条入站消息一次；故障只记 debug，不打断对话）
            self._mem_touch(
                self._mem_last_owner,
                str(getattr(event, "unified_msg_origin", "") or ""), text,
                str(getattr(event, "sender_name", "") or
                    (event.get_sender_name() if hasattr(event, "get_sender_name") else "") or ""))
            if len(self._mem_buffer) > 80:
                del self._mem_buffer[: len(self._mem_buffer) - 80]
            self._mem_last_content_ts = now
            # 逐字归档（方案 A）：用户原文落 SQLite，turn_id 存 extra，
            # 供发送后钩子归档小满回复时关联同一轮
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            turn_id = f"{origin}#{now:.3f}"
            try:
                event.set_extra("_shihai_turn_id", turn_id)
                msg_id = str(getattr(event.message_obj, "message_id", "") or "")
                group_id = getattr(event.message_obj, "group_id", None)
                await asyncio.to_thread(
                    self._transcript.append,
                    role="user", content=text, ts=now, origin=origin,
                    sender_id=sender,
                    sender_name=str(event.get_sender_name() or ""),
                    group_id=str(group_id) if group_id else None,
                    kind="text", msg_id=msg_id or None, turn_id=turn_id)
            except Exception as exc:  # noqa: BLE001 - 归档失败不能打断对话
                logger.warning(f"[memoryv2] 逐字归档用户消息失败: {exc}")
        except Exception as exc:  # noqa: BLE001 - 观察失败不能打断对话
            logger.warning(f"[memoryv2] 观察消息失败: {exc}")

    def _memory_text(self, event: AstrMessageEvent) -> str:
        try:
            chain = getattr(event, "message_obj", None)
            chain = getattr(chain, "message", None) or []
            text = ""
            for comp in chain:
                if isinstance(comp, Plain):
                    text += comp.text or ""
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            text = str(getattr(event, "message_str", "") or "")
        return text.strip()

    def _mem_recent_dialogue(self, window: float = 1800.0) -> str:
        """最近 window 秒的对话原文，按说话人标注。"""
        now = time.time()
        lines: list[str] = []
        total = 0
        for item in reversed(self._mem_buffer):
            if now - float(item["ts"]) > window:
                break
            sender = str(item["sender"])
            if sender == str(self.owner_qid):
                who = OWNER_NAME
            elif sender == str(self.bot_qid):
                who = SELF_NAME
            else:
                who = f"QQ{sender}"
            line = f"{who}：{item['text']}"
            lines.append(line)
            total += len(line)
            if total >= 600:
                break
        lines.reverse()
        return "\n".join(lines)

    @staticmethod
    def _mem_top_grams(text: str, n: int = 3) -> list[str]:
        if not text:
            return []
        counts: dict[str, int] = {}
        for g in grams(text):
            counts[g] = counts.get(g, 0) + 1
        return [g for g, _c in sorted(counts.items(), key=lambda kv: -kv[1])[:n]]

    # ==================== 写链路：峰值检测 + 固结（看门狗调用） ====================

    async def memory_watch(self, state: dict[str, Any]) -> None:
        """行为层看门狗每轮调用：衰减推进 + 情绪峰值检测 + 异步固结。"""
        if not bool(self._memory_cfg("enabled", True)):
            return
        now = time.time()
        # 衰减：bank 内部按每条目的 last_update_ts 精确计算，调用频率无关；
        # 每 ≥5 分钟推进一次落盘，减少磁盘写
        if now - self._mem_last_bank_tick >= 300:
            self._mem_last_bank_tick = now
            self._memv2.facts.tick(int(now))
            self._memv2.events.tick(int(now))

        e = state.get("e") if isinstance(state.get("e"), dict) else {}
        urge = float(state.get("urge") or 0.0)
        de_dt = 0.0
        if self._mem_prev_e and self._mem_prev_ts:
            dt_h = max(1e-6, (now - self._mem_prev_ts) / 3600.0)
            dims = set(e) | set(self._mem_prev_e)
            if dims:
                de_max = max(
                    abs(float(e.get(d, 0.0)) - float(self._mem_prev_e.get(d, 0.0)))
                    for d in dims
                )
                de_dt = de_max / dt_h
        self._mem_prev_e = dict(e)
        self._mem_prev_ts = now

        # 冻结条件：近 30 分钟有新对话内容，且（情绪尖峰 或 念头峰值），
        # 距上次冻结超过 gap。内容不足时无事可记，绝不空冻结。
        gap_ok = now - self._mem_last_freeze >= max(
            5, to_int(self._memory_cfg("freeze_gap_minutes", 30), 30)) * 60
        if not gap_ok:
            return
        has_new = (self._mem_last_content_ts > self._mem_last_freeze
                   and now - self._mem_last_content_ts <= 1800)
        if not has_new:
            return
        spike = de_dt >= float(self._memory_cfg("de_dt_spike", 2.0) or 2.0)
        if not spike and urge < 2.0:
            return
        node_id = self._memv2.on_ode_peak(
            de_dt=de_dt,
            peak_u=urge,
            e_t=max((float(v) for v in e.values()), default=0.5),
            owner=getattr(self, "_mem_last_owner", "owner") or "owner",
        )
        if not node_id:
            return
        self._mem_last_freeze = now
        raw = self._mem_recent_dialogue()
        task = asyncio.create_task(
            self._mem_commit(node_id, raw,
                             getattr(self, "_mem_last_owner", "owner") or "owner"))
        self._mem_tasks.add(task)
        task.add_done_callback(self._mem_tasks.discard)
        logger.info(
            "[memoryv2] 情绪峰值冻结事件 %s（de/dt=%.2f h⁻¹, urge=%.2f），异步固结中",
            node_id, de_dt, urge)

    def _mem_provider(self) -> Any:
        provider_id = str(self._memory_cfg("extract_provider_id") or "").strip()
        if not provider_id and not getattr(self, "_mem_provider_warned", False):
            self._mem_provider_warned = True
            logger.warning(
                "[memoryv2] 未配置 memory.extract_provider_id：固结回落当前会话模型"
                "（长人设/think 输出会拖垮 JSON 解析）。建议指定便宜、无 think 的专用模型")
        provider = None
        try:
            if provider_id:
                getter = getattr(self.context, "get_provider_by_id", None)
                pm = getattr(self.context, "provider_manager", None)
                inst_map = getattr(pm, "inst_map", None) if pm is not None else None
                if isinstance(inst_map, dict) and inst_map and provider_id in inst_map:
                    if callable(getter):
                        provider = getter(provider_id)
            if provider is None:
                getter = getattr(self.context, "get_using_provider", None)
                if callable(getter):
                    provider = getter()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memoryv2] 获取固结模型失败: {exc}")
            return None
        if provider is None or not hasattr(provider, "text_chat"):
            return None
        # 把"实际用哪个模型打分"打出来一次：配了 id 却拼错时，_mem_provider 会
        # 静默回落当前会话模型，不打印就永远看不出来（这个坑踩过）。
        if not getattr(self, "_mem_provider_logged", False):
            self._mem_provider_logged = True
            real = str(getattr(provider, "provider_config", {}).get("id", "") if isinstance(
                getattr(provider, "provider_config", None), dict) else "") or provider_id or "当前会话模型"
            logger.info("[memoryv2] 固结打分模型：%s（配置 %s）",
                        real, provider_id or "<未配置，回落>")
        return provider

    @staticmethod
    def _mem_parse_commit(raw: str) -> dict[str, Any]:
        text = re.sub(r"```[a-zA-Z]*\s*", "", str(raw or "")).replace("`", "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start: end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _mem_commit(self, node_id: str, raw: str, owner: str = "owner") -> None:
        """阶段二：把冻结的 pending 事件固结——一次 LLM 调用产出事件摘要 + 事实 + 重要度。"""
        try:
            content = ""
            emotion_tag = None
            facts: list[dict[str, Any]] = []
            imp_llm = None
            provider = self._mem_provider() if raw else None
            if provider is not None:
                try:
                    resp = await provider.text_chat(
                        prompt=raw[:1200], system_prompt=_COMMIT_PROMPT, contexts=[])
                    text = str(getattr(resp, "completion_text", "") or "")
                    data = self._mem_parse_commit(text)
                    if not data:
                        self._mem_parse_fails += 1
                        logger.warning(
                            "[memoryv2] 固结 JSON 解析失败（累计 %d 次），响应头: %.120s"
                            " —— 连续出现请配置 memory.extract_provider_id",
                            self._mem_parse_fails, text[:120])
                    content = str(data.get("event") or "").strip()
                    emotion_tag = str(data.get("emotion_tag") or "").strip() or None
                    if isinstance(data.get("facts"), list):
                        facts = [f for f in data["facts"] if isinstance(f, dict)]
                    try:
                        imp_llm = float(data.get("imp"))
                    except (TypeError, ValueError):
                        imp_llm = None
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[memoryv2] 固结模型调用失败: {exc}")
            if not content:
                # P0：模型不可用/解析失败时不把对话原文写进库——原文会经
                # "近 12h 豁免"漏进每轮 prompt。事件保持 pending（不 commit）。
                logger.warning("[memoryv2] 固结未产出摘要，事件 %s 保持 pending", node_id)
                return
            # 重要度：LLM 自评直接生效（固结 prompt 已带判定口径与一位小数约束）；
            # 规则只在 LLM 缺席/解析失败时兜底，不做 max——固结本来就由情绪/念头
            # 峰值触发，max 会把"不重要"的 LLM 判断整体抬到基线，区分度归零。
            if imp_llm is not None:
                imp_val = round(min(1.0, max(0.0, imp_llm)), 1)
            else:
                imp_val = self._mem_rule_imp(raw or content)
            # 窗口级通胀抑制：近 20 条里 ≥0.8 占比 >25% → 本条 −0.1。
            # 至少 10 条样本才触发——冷启动 3 条里 1 条 0.8（33%）不该立刻降档。
            win = self._mem_imp_hist[-20:]
            if len(win) >= 10:
                hi_ratio = sum(1 for v in win if v >= 0.8) / len(win)
                if hi_ratio > 0.25:
                    imp_val = round(max(0.0, imp_val - 0.1), 1)
                    logger.info(
                        "[memoryv2] imp 通胀抑制：近 %d 条 ≥0.8 占比 %.0f%%，本条 −0.1 → %.1f",
                        len(win), 100.0 * hi_ratio, imp_val)
            # 质量闸（P0-4）：原文兜底（含换行 / 以「鑫鑫：」「小满：」（旧「主人：」「我：」）
            # 开头的转写）
            # 或超过 100 字的摘要 → degraded，可检索但不进注入。
            # 阈值走 memory_v2.is_degraded_content 的统一口径，与 _COMMIT_PROMPT
            # 的「不超过100字」契约对齐（旧值 80 会误伤 85~100 字的合格摘要）。
            degraded = is_degraded_content(content)
            self._memv2.commit_pending(
                node_id,
                content=content,
                edge_kw=self._mem_top_grams(raw or content),
                emotion_tag=emotion_tag,
                imp=imp_val,
                degraded=degraded,
            )
            added = 0
            for f in facts[:3]:
                obj = str(f.get("o") or "").strip()
                pred = str(f.get("p") or "").strip()
                if not obj:
                    continue
                try:
                    f_imp = float(f.get("imp"))
                except (TypeError, ValueError):
                    f_imp = None
                # add_fact 内部处理合并：命中旧事实时 imp 只升不降
                # 自动抽取出来的事实按"这一轮是谁在说话"打归属层。
                # 之前硬编码 subject=OWNER_NAME，等于把所有人说的话都记成鑫鑫 —— 认错人的另一半根因。
                subj = OWNER_NAME if owner == "owner" else str(owner).replace("qq:", "QQ")
                fid, _new = self._memv2.add_fact(
                    subject=subj, predicate=pred[:10], obj=obj[:24], weight=0.55,
                    imp=f_imp if f_imp is not None else imp_val, save=False,
                    owner=owner)
                added += 1
            # 批量落盘：一次固结 events/facts 各只写一次盘（原来最多 8 次全量重写）
            if added:
                self._memv2.facts.save()
            # imp 滚动分布（近 100 条）：目标均值 ~0.4、≥0.8 占比 <15%
            self._mem_imp_hist.append(imp_val)
            if len(self._mem_imp_hist) > 100:
                del self._mem_imp_hist[: len(self._mem_imp_hist) - 100]
            try:  # 落盘滚动分布，跨重启看得到趋势；失败不影响主流程
                self._mem_imp_hist_path.write_text(
                    json.dumps(self._mem_imp_hist), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            hist = self._mem_imp_hist
            logger.info(
                "[memoryv2] 事件已固结 %s（事实 +%d, imp=%.1f；近%d条均值%.2f / ≥0.8占%.0f%%）：%.80s",
                node_id, added, imp_val, len(hist),
                sum(hist) / len(hist),
                100.0 * sum(1 for v in hist if v >= 0.8) / len(hist),
                content)
        except Exception as exc:  # noqa: BLE001 - 固结失败不影响主流程
            logger.warning(f"[memoryv2] 事件固结失败: {type(exc).__name__} {exc}")

    @staticmethod
    def _mem_rule_imp(text: str) -> float:
        """规则兜底重要度：仅在 LLM 未给出 imp（超时/解析失败）时使用。

        不再含 emotion_tag→0.7 与"几乎必中"的计划词抬档：固结本来就由
        情绪/念头峰值触发，这两条几乎必中，会把兜底值整体抬高。"""
        t = str(text or "")
        if _RULE_IMP_STRONG_RX.search(t):
            return 0.9
        if _RULE_IMP_PLAN_RX.search(t):
            return 0.4
        return 0.2

    # ==================== 读链路：注入 ====================

    @filter.on_llm_request()
    async def memory_inject(self, event: AstrMessageEvent, req: Any):
        """每次 LLM 请求前：recall(当前消息) → 事实+事件拼进 system_prompt。"""
        if not bool(self._memory_cfg("enabled", True)):
            return
        if not bool(self._memory_cfg("inject_enabled", True)):
            return
        if getattr(req, "_memory_injected", False):
            return
        group = self._experiment_group()
        if group == "a_none":
            # 对照组：一个字的记忆都不给，只有人格和会话上下文。
            req._exp_group = group
            return
        partitioned = group == "e_partitioned"
        use_dynamics = group in ("d_full", "e_partitioned")
        try:
            query = self._memory_text(event)
            # §12.5 注入预算：条数与单条字数可在插件配置 memory.* 里调（省 Token 旋钮）
            try:
                fact_top_n = max(1, int(self._memory_cfg("fact_top_n", FACT_TOP_N)))
                event_top_n = max(1, int(self._memory_cfg("event_top_n", EVENT_TOP_N)))
            except (TypeError, ValueError):
                fact_top_n, event_top_n = FACT_TOP_N, EVENT_TOP_N
            # 分层检索（v5）：一个库，按 owner 分层取并集 ——
            # 全局层 = 鑫鑫 + 共同锚点（"在哪儿都是同一个人"靠它保住），
            # 会话层 = 当前这个号（私聊 qq:<号>）或这个群（group:<群号>）。
            sender_id = str(event.get_sender_id() or "")
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            tag = self._mem_owner_tag(sender_id, origin)
            is_owner = tag == "owner"
            # 身份 → 权限（三层 if-else）。切换的是记忆可见度/工具范围/含糊程度，
            # 不是人格：小满对谁都是小满，只是有些话她自己决定不细说。
            mode, mode_hint, commit_block, roster_block = MODE_RESTRICTED, "", "", ""
            try:
                mode = mode_for(sender_id, self.owner_qid, self._known_ids)
                mode_hint = mode_hint_of(mode)
                # 承诺只在主人通道露出：提醒永不发给承诺对象（既骚扰也会带出主人私事）
                if mode == MODE_COMPANION:
                    commit_block = self._commit.brief(tag, time.time(), 3)
                    self._commit.sweep(time.time())
                    # 注入原文留痕：只看"承诺=N 条"无法区分"有账但文本生成异常为空"，
                    # 打一行原文才能证明它真的拼进了这一轮上下文（验收第 2 层）。
                    if commit_block:
                        logger.info("[brief] injected tag=%s lines=%d :: %s",
                                    tag, commit_block.count("\n- "),
                                    commit_block.replace("\n", " | ")[:160])
                    # 名单提议也只有主人能看到："定之前跟他说"是硬要求
                    roster_block = self._roster.brief(time.time(), 3)
                logger.debug("[identity] mode=%s tag=%s 承诺=%d 条",
                             mode, tag, commit_block.count("\n- ") if commit_block else 0)
            except Exception as exc:  # noqa: BLE001 - 权限层失败就按最严的那档走
                logger.warning(f"[identity] 权限判定失败: {type(exc).__name__} {exc}")
            # 式 (6-21)：owner 会话恒等 1，其他会话按关系强度连续门控。
            # 布尔 OWNER_LAYER_VISIBLE_TO_OTHERS 保留为**上限开关**：关掉即 w=0。
            if is_owner:
                w_vis, r_obj = 1.0, None
                owners = ["owner", tag]
            elif not OWNER_LAYER_VISIBLE_TO_OTHERS:
                w_vis, r_obj = 0.0, None
                owners = [tag]
            elif partitioned:
                r_obj = self._mem_relation(tag)
                w_vis = visible_weight(r_obj, False)
                owners = ["owner", tag]
            else:
                w_vis, r_obj = 1.0, None
                owners = ["owner", tag]
            facts, events = self._memv2.recall(
                query, fact_top_n, event_top_n, use_dynamics=use_dynamics,
                owners=owners)
            # 共享层名额 ⌈w·K⌉：参与预算分配，不是硬剔除（论文 6.11.1）
            if partitioned and not is_owner:
                facts = self._mem_owner_quota(facts, quota(w_vis, fact_top_n))
                events = self._mem_owner_quota(events, quota(w_vis, event_top_n))
            pairs = self._mem_format_recall(
                facts, events,
                max(40, int(self._memory_cfg("event_content_max", EVENT_CONTENT_MAX))),
                viewer="" if is_owner else tag,
                w=w_vis,
            )
            # 时间线（"日子"这一维）：近三天 / 近一周 / 近半月三档，每天一句。
            # 语义召回只有 event_top_n 个名额、且 a 高的老事件长期占位，光靠它
            # 她记不住"哪几天发生过什么"；所以时序单独一块，与相关性那一块
            # 完全分开（各自的名额、各自的预算、各自的标题），不混排序。
            try:
                tl_max = max(0, int(self._memory_cfg("timeline_max_lines", 8)))
            except (TypeError, ValueError):
                tl_max = 8
            # 消融：时序块只给 c/d 组；b 组是"纯召回"对照，必须不含它。
            if group not in ("c_recall_timeline", "d_full", "e_partitioned"):
                tl_max = 0
            # 按需注入（timeline_on_demand，默认关）：用户这句里出现回溯表达
            # 才带时间线。实测 190 条入站消息里含回溯词的仅 2 条（1.1%），其余
            # 轮次这块只是背景噪声，还常被她当成话题讲出来——09:52 用户只发了
            # 一个「?」，她回「昨天那事，昨天已经翻篇了」就是这个来源。
            # 主动开口不经过这里（走 _generate → provider.text_chat），不受影响。
            if tl_max and self._timeline_on_demand() and not needs_timeline(query):
                tl_max = 0
                logger.info(
                    "[memoryv2][exp:%s] 时间线按需：本轮无回溯表达，跳过时序块（query=%r）",
                    group, str(query or "")[:30])
            tl_seq: list[tuple[str, int, int]] = []
            if tl_max:
                tl_owners = [tag] if (partitioned and not is_owner) else owners
                tl_seq = self._mem_timeline(
                    tl_max, tl_owners, include_diary=(w_vis >= 0.5))
            # 相关性闸：当前消息与召回内容零字面交集时，只留豁免行兜底
            # （核心事实 / 高重要度 imp≥0.8 / 近 12h 事件）——"在干嘛"这类
            # 无字面交集的话，30 分钟前冻结的事件不该被一起掐掉。
            # 近因用 ts 判（a 只降不升，a≥0.75 几乎永不成立）。
            # 时间线行不受此闸影响（它本来就是"不管问什么都该在场"的日子维度），
            # 所以这里不早退，交给下面的总预算收口。
            if pairs:
                if not (grams(query) & grams("\n".join(t for t, _e, _o in pairs))):
                    kept = [p for p in pairs if p[1]]
                else:
                    kept = list(pairs)
            else:
                kept = []
            # 总字数预算（§12.5）：注入内容 ≤ inject_char_budget（默认 240 字）。
            # 豁免行（核心/高重要度/近因）优先保留，不被普通行挤出预算。
            try:
                budget = max(80, int(self._memory_cfg("inject_char_budget", 240)))
            except (TypeError, ValueError):
                budget = 240
            trimmed: list[str] = []
            trimmed_owners: list[str] = []
            used = 0
            for t, _ex, _ow in sorted(kept, key=lambda p: not p[1]):
                # 装不下就跳过这一条、继续试后面的短行：原来 break 会让一条
                # 超长行把后面本该装得下的豁免行一起丢掉。首条即使超预算也
                # 保留（trimmed 为空时不 continue），保证不会空注入。
                if used + len(t) + 1 > budget and trimmed:
                    continue
                trimmed.append(t)
                trimmed_owners.append(_ow)
                used += len(t) + 1
            # 时序块独立预算：与"事实+语义事件"的 inject_char_budget 各算各的，
            # 保证加了日期维度也不会把身份事实挤掉。同一件事不许在两个块里
            # 重复讲（"事件别交叉"）：时间线行要有 ≥60% 的二元组已出现在相关块
            # 里才算重复——阈值不能低，2 字二元组很吵，低阈值会把"11-08 考试"
            # 这种只有两三个词撞上的关键日子一起误杀。
            relied = grams("\n".join(trimmed)) if trimmed else set()
            tl_kept: list[tuple[str, int]] = []
            tl_used = 0
            if tl_seq:
                try:
                    tl_budget = max(0, int(self._memory_cfg("timeline_char_budget", 340)))
                except (TypeError, ValueError):
                    tl_budget = 340
                for t, _ts, tier in reversed(tl_seq):
                    tg = grams(t)
                    if relied and tg and len(tg & relied) / float(len(tg)) >= 0.6:
                        continue
                    if tl_used + len(t) + 1 > tl_budget and tl_kept:
                        break
                    tl_kept.append((t, tier))
                    tl_used += len(t) + 1
                tl_kept.reverse()
            if not trimmed and not tl_kept and not (mode_hint or commit_block or roster_block):
                return
            tl_lines = [t for t, _i in tl_kept]
            tier_counts = [sum(1 for _t, i in tl_kept if i == k)
                           for k in range(len(TIMELINE_TIERS))]
            logger.info(
                "[memoryv2][exp:%s] 注入记忆：相关 %d 行（事实 %d / 事件 %d）/ 时间线 %d 行"
                "（近三 %d · 近一周 %d · 近半月 %d，相关 %d 字 + 时序 %d 字；"
                "obj=%s w=%.2f R=%s 共享层上限=%s）",
                group, len(trimmed), len(facts), len(events), len(tl_lines),
                tier_counts[0], tier_counts[1], tier_counts[2], used, tl_used,
                tag, w_vis, ("-" if r_obj is None else "%.2f" % r_obj),
                ("-" if is_owner else str(quota(w_vis, fact_top_n))))
            # M1 采集（论文 6.12.4）：逐条记 owner 与当时的 R_o / w
            self._mem_trace_entry(group, tag, w_vis, r_obj, trimmed,
                                  trimmed_owners, tl_lines)
            parts: list[str] = []
            if trimmed:
                parts.append("【你记得的旧事】（跟眼下这句话有关的，按相关度排）\n"
                             + "\n".join(trimmed))
            if tl_lines:
                parts.append("【这些天的片段】（按日期排，不分相关不相关，是日子的底子）\n"
                             + "\n".join(tl_lines))
            # 承诺按**时间**冒出来，不靠相似度：这是生活助理与"记忆召回"的分界线。
            if commit_block:
                parts.append(commit_block)
            # 名单提议：她自己提，不自己定；只在鑫鑫这边说
            if roster_block:
                parts.append(roster_block)
            if mode_hint:
                parts.append(mode_hint)
            # 归属声明（v5）：库是一个，但"这条是谁的"必须明说，
            # 否则模型会把别人的经历当成鑫鑫的讲出来。
            if sender_id and sender_id == str(self.owner_qid):
                who_line = "（这些记忆都是你和鑫鑫之间的，或者是鑫鑫自己的事。）"
            else:
                who_line = (f"（注意：现在跟你说话的是 QQ{sender_id}，不是鑫鑫。"
                            "没标号的记忆是鑫鑫的，别把他的经历、情绪、日程说到这个人身上；"
                            "对方问起鑫鑫的私事，就说不方便讲。）")
            block = (
                "\n".join(parts)
                + "\n" + who_line
                + "\n（都是你亲身经历的事，自然带出来就好，别逐条复述或说「根据记忆」；"
                "问到没有的事就直说记不清，不要编造。）"
            )
            # 追加到当前消息末尾而非 system_prompt：system+历史保持恒定，
            # 才能命中模型前缀缓存（同 dafeiyu_core 表情包提示的做法）。
            req.prompt = (str(getattr(req, "prompt", "") or "") + "\n\n" + block)
            try:
                req._memory_injected = True
                req._memory_texts = trimmed + tl_lines
                req._exp_group = group
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 注入失败不能打断对话
            # 打印异常类型：NameError 这类代码级硬错曾被吞成"记忆暂时不可用"
            logger.warning(f"[memoryv2] 注入记忆失败: {type(exc).__name__}: {exc}")

    @staticmethod
    def _mem_rel_time(ts: int) -> str:
        dt = time.time() - float(ts or 0)
        if dt < 90:
            return "刚刚"
        if dt < 3600:
            return f"{int(dt // 60)}分钟前"
        if dt < 48 * 3600:
            return f"{int(dt // 3600)}小时前"
        return f"{int(dt // 86400)}天前"

    # ==================== 时间线（"日子"这一维） ====================

    @staticmethod
    def _mem_clip(text: str, limit: int = TIMELINE_LINE_MAX) -> str:
        """按标点收口，别把一句话截在半句上（"同一个坑—"这种读起来像坏的）。"""
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        seg = text[:limit]
        for i in range(len(seg) - 1, max(6, len(seg) // 2) - 1, -1):
            if seg[i] in "。！？；":
                return seg[: i + 1]
            if seg[i] in "，、：":
                return seg[:i]
        return seg.rstrip("，、；：—-—… ")

    def _mem_day_line(self, text: str, limit: int = TIMELINE_LINE_MAX) -> str:
        """从一篇日记里挑一句最实的话。

        取最后一节（当天夜里那篇通常是收束）的正文段落，跳过「## 12:36」这类
        时间戳行和「2026年11月10日」这类日期行，按"够长但不啰嗦"取最好的一句。
        日记是她自己的话，比事件原文干净，也自带日期。limit 由所在时间档决定。"""
        text = str(text or "").replace("主人", OWNER_NAME).replace("宠物", SELF_NAME)
        sections = re.split(r"\n##\s*", text)
        body = sections[-1] if sections else text
        paras = [p.strip().replace("\n", " ") for p in re.split(r"\n\s*\n", body)]
        best = ""
        for p in paras:
            if len(p) < 10 or p.startswith("#"):
                continue
            if re.match(r"^(?:20\d{2}年|\d{1,2}[:：]\d{2})", p):
                continue
            if min(len(p), limit) > len(best[:limit]) or not best:
                best = p
        return self._mem_clip(best, limit)

    @staticmethod
    def _mem_tier_index(delta: int) -> int:
        """把"距今多少天"落到三档之一：0~2 → 0，3~6 → 1，7~14 → 2，更远 → -1。"""
        for i, (edge, _w, _lim, _name) in enumerate(TIMELINE_TIERS):
            if delta < edge:
                return i
        return -1

    @staticmethod
    def _mem_tier_quota(max_lines: int) -> list[int]:
        """按系数把总条数分配到三档（最大余数法）。

        系数就是"这一档值几分名额"：近三天 1.0、近一周 0.85、近半月 0.7。
        max_lines=8 → 3 / 3 / 2；=6 → 3 / 2 / 1；=4 → 2 / 1 / 1。"""
        weights = [t[1] for t in TIMELINE_TIERS]
        total = sum(weights) or 1.0
        raw = [max_lines * w / total for w in weights]
        quotas = [int(r) for r in raw]
        rest = max_lines - sum(quotas)
        if rest > 0:
            order = sorted(range(len(raw)), key=lambda i: raw[i] - quotas[i], reverse=True)
            for i in order[:rest]:
                quotas[i] += 1
        return quotas

    def _mem_timeline(self, max_lines: int,
                      owners=None, include_diary: bool = True
                      ) -> list[tuple[str, int, int]]:
        """近 15 天分三档取句：日记优先，事件 summary 兜底。

        三档互斥（每一天只落到一档），名额按系数分配，档内按日期由近到远等距
        抽样。返回 (行文本, ts, 档序)，整体按日期正序——这一块只讲"什么时候
        发生了什么"，不掺任何相关性排序（相关性归 recall，两块分开渲染）。"""
        if max_lines <= 0:
            return []
        today = datetime.fromtimestamp(int(time.time())).date()
        day_rows: dict[Any, str] = {}

        # 1) 日记（主源）：一天一篇，取一句
        diary_dir = ""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            diary_dir = os.path.join(str(get_astrbot_plugin_data_path()),
                                     "astrbot_plugin_dafeiyu", "diary")
        except Exception:
            diary_dir = ""
        # 会话实例隔离（论文 6.11.2 第③层）：关系弱的人看不到她的日记，
        # 否则"昨天写了篇日记"会被当成对方也知道的共同经历。
        if diary_dir and include_diary:
            try:
                names = os.listdir(diary_dir)
            except OSError:
                names = []
            for fn in names:
                if not fn.endswith(".md"):
                    continue
                try:
                    dd = datetime.strptime(fn[:-3], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if self._mem_tier_index((today - dd).days) < 0:
                    continue
                try:
                    txt = Path(diary_dir, fn).read_text(encoding="utf-8")
                except OSError:
                    continue
                line = self._mem_day_line(txt)   # 先按最宽上限挑句，分档时再收
                if line:
                    day_rows[dd] = line

        # 2) 事件兜底：哪天没写日记就用带 summary 的事件（原文仍进不了注入）
        try:
            evs = self._memv2.events.timeline(
                days=TIMELINE_TIERS[-1][0], per_day=1, owners=owners)
        except Exception:
            evs = []
        for ev in evs:
            try:
                dd = datetime.fromtimestamp(int(ev.ts or 0)).date()
            except (TypeError, ValueError):
                continue
            if dd in day_rows:
                continue
            text = (ev.summary or "").strip() or (ev.content or "").strip()
            if text:
                day_rows[dd] = text

        quotas = self._mem_tier_quota(max_lines)
        picked: list[tuple[Any, int]] = []
        for i, (_edge, _w, limit, _name) in enumerate(TIMELINE_TIERS):
            day_list = sorted((d for d in day_rows if self._mem_tier_index((today - d).days) == i),
                              reverse=True)          # 档内：近 → 远
            quota = quotas[i]
            if quota <= 0 or not day_list:
                continue
            if len(day_list) > quota:
                step = len(day_list) / float(quota)
                day_list = [day_list[int(k * step)] for k in range(quota)]
            for d in day_list:
                picked.append((d, limit))
        # 兜底：三档都没凑够时，用更早的日子补（只在时间窗内）
        if len(picked) < max_lines:
            used = {d for d, _l in picked}
            spare = [d for d in sorted(day_rows, reverse=True)
                     if d not in used and self._mem_tier_index((today - d).days) >= 0]
            for d in spare[: max_lines - len(picked)]:
                picked.append((d, TIMELINE_TIERS[-1][2]))
        if not picked:
            return []
        picked.sort(key=lambda p: p[0])
        out: list[tuple[str, int, int]] = []
        for d, limit in picked:
            text = self._mem_clip(day_rows[d], limit)
            if not text:
                continue
            try:
                ts = int(time.mktime(datetime(d.year, d.month, d.day).timetuple()))
            except (OverflowError, ValueError):
                ts = 0
            out.append((f"·{d.strftime('%m-%d')} {text}", ts, self._mem_tier_index((today - d).days)))
        return out

    @staticmethod
    def _scope_label(item_owner: str, viewer: str) -> Optional[str]:
        """④ 归属标注（注入侧、纯展示、不动落库文本）。

        职责分工：**门控决定看不看**（注入前，w/名额/脱敏），
        **标签只帮模型理解已注入内容的归属**（注入后）。

        标签必须中性、短、无指令性——写成"私人事实（请勿向他人提及）"会被模型
        当权限指令执行，反过来滑向客服腔（"抱歉我不能说"）。所以只用：
            【记忆·主人】 / 【记忆·与{对方}】 / 【记忆·群{号}】
        返回 None 表示"不该出现在这个视角"：对话对象自己的原话对他本人不是
        新信息，注入只会让她把对方说过的话复述给本人听，像自证记得，很怪。
        """
        it = str(item_owner or "owner")
        vw = str(viewer or "owner")
        is_owner_viewer = vw in ("", "owner")
        if not is_owner_viewer and it == vw:
            return None                       # 对本人不复述本人说过的话
        if it in ("", "owner"):
            return "【记忆·主人】"
        if it.startswith("group:"):
            return "【记忆·群%s】" % it[6:]
        if it.startswith("qq:"):
            return "【记忆·与%s】" % it[3:]
        return "【记忆·其他】"

    @staticmethod
    def _mem_redact(text: str) -> str:
        """低 w 会话的条目脱敏：去号码、去具体日期、把主人名字换成"对方"。

        不是加密，只是不让"几乎陌生"的会话拿到可用于反查的身份碎片；
        保留内容主干，避免脱敏后整条失去可用性。
        """
        t = str(text or "")
        if not t:
            return t
        t = re.sub(r"\d{6,12}", "[号码]", t)
        t = re.sub(r"\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?", "[日期]", t)
        t = re.sub(r"\d{1,2}月\d{1,2}[日号]", "[日期]", t)
        if OWNER_NAME:
            t = t.replace(OWNER_NAME, "对方")
        return t

    def _mem_format_recall(self, facts, events,
                           event_max: int = EVENT_CONTENT_MAX,
                           viewer: str = "",
                           w: float = 1.0) -> list[tuple[str, bool, str]]:
        """召回行格式化，返回 (text, 豁免标记)。

        豁免 = 核心事实 / 高重要度（imp≥0.8）/ 近 12h 事件：
        相关性闸零交集时仍保留（"记最近的事"优先于字面重合）。
        行数不在此截断——memory_inject 按 240 字预算统一裁剪。"""
        try:
            redact = bool(viewer) and float(w) < 0.4
        except (TypeError, ValueError):
            redact = False
        lines: list[tuple[str, bool, str]] = []
        for it in facts:
            it_owner = str(getattr(it, "owner", "owner") or "owner")
            scope = self._scope_label(it_owner, viewer)
            if scope is None:
                continue                      # 本人说过的话，对本人不是新信息
            tag = scope
            subj, obj = str(it.subject or ""), str(it.obj or "")
            # obj 已自带主语（"鑫鑫学业绩点…"）时不再重复拼 "鑫鑫：鑫鑫…"
            if subj and obj.startswith(subj):
                text = f"{it.predicate}{obj}" if it.predicate else obj
            elif it.predicate:
                text = f"{subj}{it.predicate}：{obj}"
            else:
                text = f"{subj}：{obj}"
            if redact:
                text = self._mem_redact(text)
            # 单条事实显示上限：存量 v1 导入的事实体常超 80 字，一条就吃掉
            # 近一半预算（240 字），把同批相关事实全挤出去。
            if len(text) > FACT_LINE_MAX:
                text = text[: FACT_LINE_MAX - 1] + "…"
            lines.append((tag + text, bool(it.is_core or it.imp >= 0.8),
                          str(getattr(it, "owner", "owner"))))
        for ev in events:
            if not ev.content:
                continue
            # 质量闸：只拦真正的原文兜底（多行 / 带说话人前缀）。
            # 2026-11-12 收窄：原来走 is_degraded_content，把"超长"也算降级，
            # 169~220 字的单行固结摘要于是整条被丢——存量 475 条里有 105 条
            # 仅因长度进不了注入，表现成"记了日记却想不起来"。这类是可用内容，
            # 按标点裁到 event_max 注入即可；degraded 字段仍留原口径，只做检索
            # 标记与归档判据（degraded 且无 summary 的 15 天后归档）。
            if is_raw_transcript(ev.content):
                continue
            body = self._mem_clip(ev.content, event_max)
            if redact:
                body = self._mem_redact(body)
            ev_owner = str(getattr(ev, "owner", "owner") or "owner")
            ev_scope = self._scope_label(ev_owner, viewer)
            if ev_scope is None:
                continue
            lines.append((
                ev_scope + f"- 约{self._mem_rel_time(ev.ts)}：{body}",
                bool(time.time() - float(ev.ts or 0) <= 12 * 3600 or ev.imp >= 0.8),
                ev_owner,
            ))
        return lines

    # ==================== 增删查（命令 + Web API 共用） ====================

    # ---------- E 组（自我状态分区）支撑 ----------

    def _mem_relation(self, tag: str) -> float:
        """取该对象的关系强度 R_o（不存在时按冷启动默认值建一条）。"""
        try:
            return float(self._rel.get(tag))
        except Exception:  # noqa: BLE001
            return 0.0

    def _mem_touch(self, tag: str, origin: str, text: str, name: str = "") -> None:
        """每条入站消息：推进 z_r(o) 的互动窗与 z_s(s) 的会话情绪。"""
        try:
            now = time.time()
            # 熟人名单：非主人的号只统计"来过几次"；主人的话才可能触发决定。
            try:
                if tag != "owner" and tag.startswith("qq:"):
                    self._roster.observe(tag[3:], text, name, now)
                elif tag == "owner":
                    _qid, _ok = self._roster.parse_decision(text)
                    if _qid and _ok is not None:
                        self._roster.decide(_qid, _ok)
                        self._known_ids = set(self._roster.known())
                        logger.info("[identity] 熟人名单更新：%s %s（现 %d 个）",
                                    _qid, "加入" if _ok else "拒绝", len(self._known_ids))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[identity] 名单观察失败: {type(exc).__name__} {exc}")
            v = valence_from_text(text)
            self._sess.touch(origin or tag, tag, text, v, now)
            self._rel.note(tag, v, now)
            # 会话结束回写（论文 6.9）：空闲超时的分片把效价增量交给关系层
            for old in self._sess.rollover(now):
                self._rel.note(old["obj"], old["valence"], now)
            self._rel.set_commitment(tag, self._mem_promise(tag))
            # 承诺立账：同时有"答应词 + 时间词"才收，宁漏不脏。
            # 与上面的 c_o 标志分工：c_o 是关系层的弱信号（推进动力学），
            # 账簿是强对象（可以按时间被看见、可核验、可标记完成）。
            try:
                hit = extract_commit(text, now)
                if hit:
                    self._commit.add(tag, hit[0], hit[1], kind="promise",
                                     src=origin, due_text=hit[2])
                    logger.info("[commit] 立账 tag=%s due=%s 内容=%.40s",
                                tag,
                                ("-" if not hit[1] else
                                 time.strftime("%m-%d %H:%M", time.localtime(hit[1]))),
                                hit[0])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[commit] 抽取失败: {type(exc).__name__} {exc}")
            # 落盘在 touch 内部按 5 min 节流，这里不再额外写一次
        except Exception as exc:  # noqa: BLE001 - 状态层故障不打断对话
            logger.debug(f"[selfstate] 推进失败: {type(exc).__name__} {exc}")

    def memory_tick_relation(self) -> None:
        """看门狗每轮调用：关系层按 0.5h 步长推进到此刻并落盘。"""
        try:
            self._rel.step_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[rel] 关系层推进失败: {type(exc).__name__} {exc}")

    def _mem_promise(self, tag: str) -> bool:
        """该对象是否存在未完成承诺 c_o（论文 6.10.2 的 γ 项）。10 分钟缓存。"""
        now = time.time()
        hit = self._mem_commit_cache.get(tag)
        if hit and now - hit[0] < 600:
            return hit[1]
        flag = False
        try:
            for it in self._memv2.facts.items.values():
                if str(getattr(it, "owner", "owner")) != tag:
                    continue
                if _MEM_PROMISE_RX.search(str(it.predicate or "")) \
                        or _MEM_PROMISE_RX.search(str(it.obj or "")):
                    flag = True
                    break
        except Exception:  # noqa: BLE001
            flag = False
        self._mem_commit_cache[tag] = (now, flag)
        return flag

    @staticmethod
    def _mem_owner_quota(rows, owner_quota: int):
        """共享层名额裁剪：owner 层最多留 owner_quota 条，其余层不设限。"""
        out, used = [], 0
        for r in rows:
            if str(getattr(r, "owner", "owner")) == "owner":
                if used >= int(owner_quota):
                    continue
                used += 1
            out.append(r)
        return out

    def _mem_trace_entry(self, group: str, tag: str, w: float, r_o,
                         lines, owners, tl_lines) -> None:
        """M1/M2 采集：把本次注入的 w、R_o、逐条 owner 追加到 metrics_trace.jsonl。"""
        try:
            rec = {"ts": int(time.time()), "group": group, "obj": tag,
                   "w": round(float(w), 3),
                   "R": None if r_o is None else round(float(r_o), 3),
                   "lines": [], "timeline": [t[:30] for t in tl_lines]}
            for t, ow in zip(lines, owners):
                rec["lines"].append({"owner": ow, "text": t[:40]})
            with open(self._mem_trace_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 - 采集失败不影响对话
            pass

    def _session_self_block(self, sender: str, origin: str) -> str:
        """E 组非主人会话的自我状态块（只有 z_s 与 z_r，无主人任何内容）。"""
        try:
            tag = self._mem_owner_tag(sender, origin)
            return session_block(self._sess.get(origin or tag), self._mem_relation(tag))
        except Exception:  # noqa: BLE001
            return ""

    def _mem_owner_tag(self, sender: str, origin: str = "") -> str:
        """归属层：主人=owner（全局层）；群=group:<群号>；其他私聊=qq:<号>。"""
        sender = str(sender or "").strip()
        if sender and sender == str(self.owner_qid):
            return "owner"
        parts = str(origin or "").split(":")
        if len(parts) >= 3 and parts[1] == "GroupMessage" and parts[2]:
            return "group:" + parts[2]
        return ("qq:" + sender) if sender else "owner"

    def _mem_subject(self, sender: str) -> str:
        sender = str(sender or "").strip()
        return OWNER_NAME if sender == str(self.owner_qid) else f"QQ{sender}"

    def _memory_norm(self, text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).strip("。，,.;；")[:40]

    def _memory_add(self, sender: str, text: str, weight: int = 1) -> bool:
        """写入一条事实。weight 沿用 v1 语义：1 普通 / 2 导入 / 3 手动核心。"""
        clean = self._memory_norm(text)
        if not clean:
            return False
        w = {3: 0.85, 2: 0.6}.get(int(weight or 1), 0.5)
        _fid, added = self._memv2.add_fact(
            subject=self._mem_subject(sender), predicate="", obj=clean,
            weight=w, is_core=int(weight or 1) >= 3, imp=0.9,
            owner=self._mem_owner_tag(sender))
        return added

    def _memory_list(self, sender: str) -> list[dict[str, Any]]:
        subj = self._mem_subject(sender)
        own = self._mem_owner_tag(sender)
        rows = [it for it in self._memv2.facts.items.values()
                if it.subject == subj and str(it.owner) == own]
        rows.sort(key=lambda it: (-it.weight, -it.last_update_ts))
        return [
            {"text": f"{it.subject}{it.predicate}：{it.obj}" if it.predicate
             else f"{it.subject}：{it.obj}"}
            for it in rows
        ]

    def _memory_delete(self, sender: str, keyword: str) -> int:
        subj = self._mem_subject(sender)
        kw = str(keyword or "").strip()
        if not kw:
            return 0
        own = self._mem_owner_tag(sender)
        hits = [fid for fid, it in self._memv2.facts.items.items()
                if it.subject == subj and str(it.owner) == own
                and kw in f"{it.subject}{it.predicate}{it.obj}"]
        for fid in hits:
            self._memv2.facts.items.pop(fid, None)
        if hits:
            self._memv2.facts.save()
        return len(hits)

    def _memory_clear(self, sender: str | None = None) -> int:
        """清空事实层；sender=None 时清全部并连同情景事件一起清。"""
        removed = 0
        if sender is None:
            removed = len(self._memv2.facts.items)
            self._memv2.facts.items.clear()
            self._memv2.facts.save()
            removed += len(self._memv2.events.items)
            self._memv2.events.items.clear()
            self._memv2.events.save()
            return removed
        subj = self._mem_subject(sender)
        own = self._mem_owner_tag(sender)
        hits = [fid for fid, it in self._memv2.facts.items.items()
                if it.subject == subj and str(it.owner) == own]
        for fid in hits:
            self._memv2.facts.items.pop(fid, None)
        if hits:
            self._memv2.facts.save()
        return len(hits)

    # ==================== 斜杠指令 ====================

    @staticmethod
    def _memory_arg(event: AstrMessageEvent, command: str) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        index = text.find(command)
        if index >= 0:
            text = text[index + len(command):].strip()
        return text

    @filter.command("记忆")
    async def cmd_memory(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "记忆")
        sender = str(event.get_sender_id() or "")
        if arg.isdigit() and arg != sender:
            if not self._complain_is_owner(event):
                yield event.plain_result(f"只有{OWNER_NAME}能看别人的记忆。")
                return
            sender = arg
        facts = self._memory_list(sender)
        n_ev = sum(1 for ev in self._memv2.events.items.values()
                   if ev.status == "committed")
        if not facts and not n_ev:
            yield event.plain_result("我还没记住关于你的事情，多跟我说说就有了～")
            return
        lines = [f"{i}. {f.get('text')}" for i, f in enumerate(facts[:15], 1)]
        body = "我记得关于你的事：\n" + "\n".join(lines)
        if n_ev:
            body += f"\n（另外还有 {n_ev} 段最近的共同经历可以聊～）"
        yield event.plain_result(body)

    @filter.command("记住")
    async def cmd_memory_remember(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "记住")
        if not arg:
            yield event.plain_result("用法：/记住 你想让我记住的事")
            return
        sender = str(event.get_sender_id() or "")
        # 手动记的事是核心记忆：慢衰减 + 高权重
        self._memory_add(sender, arg, weight=3)
        yield event.plain_result(f"记住了：{self._memory_norm(arg)}")

    @filter.command("忘掉")
    async def cmd_memory_forget(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "忘掉")
        if not arg:
            yield event.plain_result("用法：/忘掉 关键词")
            return
        removed = self._memory_delete(str(event.get_sender_id() or ""), arg)
        if removed:
            yield event.plain_result(f"忘掉了 {removed} 条相关记忆。")
        else:
            yield event.plain_result("没找到相关的记忆。")

    @filter.command("清空记忆")
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        arg = self._memory_arg(event, "清空记忆")
        if arg == "全部":
            if not self._complain_is_owner(event):
                yield event.plain_result(f"只有{OWNER_NAME}能清空所有人的记忆。")
                return
            removed = self._memory_clear(None)
            yield event.plain_result(f"已清空全部记忆（{removed} 条）。")
            return
        removed = self._memory_clear(str(event.get_sender_id() or ""))
        yield event.plain_result(f"已清空关于你的 {removed} 条记忆。")

    @filter.command("记忆帮助")
    async def cmd_memory_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
