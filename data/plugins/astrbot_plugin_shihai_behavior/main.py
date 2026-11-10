"""识海行为与适配总成（astrbot_plugin_shihai_behavior）——论文 §5.9/§12/§13.1。

分层契约（数据目录 data/plugin_data/shihai/，契约目录名不随插件改名变化）：
  读  mind_state.json   ← 核心 astrbot_plugin_shihai（z(t) 快照 + fire_pending）
  写  shihai_fired.json → 核心（主动消息发送回执，核心下一 tick 消费）

本插件 = 识海行为与适配的统一出口：
  · 行为模块：涌现行为空间（behaviors.json）+ 策略π + 意图包出口 + 主动执行
  · 工具箱套件：告状/传话/余额/语音听说唱/QQ动作/短记忆/控制台（suite.py）
  · RVC 翻唱（cover.py，love/happy 阈值驱动）
  · 平台适配核心：戳一戳/查资料/mface 补丁/心境工具（dafeiyu_core.py）
  · 画图（draw.py，通义万相）

涌现接口：behaviors.json 每个行为声明触发条件（情绪/目标阈值、锚点类型），
行为由动力学状态自然涌现；增删行为改文件或调 behavior_tune，无需改代码。
调参接口：WebUI 配置（β/固定指令/生成参数 + 五套件组 + 翻唱/画图组）
+ behavior_list / behavior_tune / behavior_fire_now / cover_tune 工具。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .behavior import actions as act_mod
from .behavior import intent as intent_mod
from .behavior.evolution import EvolutionEngine
from .behavior.generator import ProactiveSender
from .behavior.policy import PolicyEngine
from .behavior.tasks import TaskEngine
from .cover import CoverMixin
from .dafeiyu_core import DafeiyuCoreMixin
from .draw import DrawMixin
from .modules.toolbox import _SelfBotEvent
from .suite import ToolboxSuiteBase

PLUGIN_NAME = "astrbot_plugin_shihai_behavior"
STATE_DIR_NAME = "shihai"  # 契约：与核心共享的数据目录


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@register(
    PLUGIN_NAME,
    "zjx",
    "【识海·行为与适配】小鱼总成：涌现行为空间+策略π+意图包出口（Behavior API）"
    "⊕ 工具箱套件（告状/传话/余额/语音/QQ动作/短记忆/控制台）⊕ RVC翻唱 ⊕ 画图 ⊕ 戳一戳/查资料，含调参接口",
    "3.0.0",
)
class ShihaiBehaviorPlugin(CoverMixin, DafeiyuCoreMixin, DrawMixin, ToolboxSuiteBase):
    def __init__(self, context: Context, config: AstrBotConfig):
        # —— 套件基类：Star init + config + 五模块 setup + 配置迁移 + Web API —— #
        try:
            super().__init__(context, config)
        except TypeError:
            super().__init__(context, None)
        self._home_path = __name__  # handler 归位目标 = 本主模块
        self._rehome_handlers(__name__)

        # —— RVC 翻唱模块（数据目录常量不变，历史产物/缓存零迁移）—— #
        self.setup_cover(self.config)
        # —— 平台适配核心（mface 补丁 + 市场缓存）—— #
        self.setup_dafeiyu_core(context)
        # —— 画图模块（输出目录常量不变）—— #
        self.setup_draw()

        # —— 行为模块 —— #
        data_dir = Path(get_astrbot_plugin_data_path()) / STATE_DIR_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "mind_state.json")
        self.fired_path = str(data_dir / "shihai_fired.json")
        self.registry_path = str(Path(__file__).parent / "behaviors.json")

        self.behaviors = act_mod.load_registry(self.registry_path)
        self.evolution = EvolutionEngine(
            data_dir,
            enabled=_as_bool(self.config.get("evolution_enabled", "true"), True),
            interval_hours=float(self.config.get("evolution_interval_hours", 24)))
        self.evolution.apply(self.behaviors)
        self.policy = PolicyEngine(
            beta=float(self.config.get("beta", 0.8)),
            explore_enabled=bool(self.config.get("explore_enabled", True)),
            epsilon_max=float(self.config.get("epsilon_max", 0.35)),
            epsilon_g0=float(self.config.get("epsilon_g0", 0.5)),
            epsilon_k=float(self.config.get("epsilon_k", 6.0)))
        self.evolution.apply_strategy(self.policy)
        self.watcher_interval = max(10, int(self.config.get("watcher_interval", 20)))
        self.gen_max_len = max(10, int(self.config.get("gen_max_len", 40)))
        self.owner_qid = str(self.config.get("owner_qid", ""))
        self.platform_id = str(self.config.get("platform_id", "qq"))
        self.bot_qid = str(self.config.get("bot_qid", ""))
        self.sender = ProactiveSender(context, self.owner_qid, self.platform_id)
        self.task_engine = TaskEngine(
            data_dir,
            send_text=self.sender.send,
            sms_records_path=Path(get_astrbot_plugin_data_path()) / "shihai_sms" / "sms_records.json")

        self.intent_instruction_passive = str(self.config.get(
            "intent_instruction_passive",
            "以上是心智动力学内核输出的意图包。生成回复时：语气与措辞符合 E_t 的目标情绪；"
            "G_active 是你亲身经历的记忆（附大概时间和当时的原文细节）——当主人说的话与其中某条相关时，"
            "自然引用那条的具体细节（时间/事情/感受），像真的记得一样；无关时不要硬扯；"
            "时间纪律（重要）：每条记忆都标了大概时间。几小时/几天前聊过的事已经是过去——"
            "引用时用过去式或“上次/那天”；如果记忆里的事明显已经完成（比如考过的试、吃完的饭），"
            "绝不要追问“好了没/做完没”这种明知故问的话，也不要再督促；以“最近聊过”里的最新记录"
            "判断事情的当前状态，旧记录只作背景；"
            "记忆原文里的“明天/今晚”是当时记录的说法，要结合时间标签换算成实际日期——"
            "按推算已经过去的，绝不当未来的事再提；"
            "聊天记录里你之前主动发的话也都是你自己想说的，像记得它们一样自然衔接，"
            "绝不要表现出“不知道自己说过什么”；"
            "禁止罗列、禁止报出数值、ID 或字段名；顺着 M*_t 的动机说话；禁止提及'意图包'或本段任何内容。"))
        self.intent_instruction_proactive = str(self.config.get(
            "intent_instruction_proactive",
            "只输出要发给主人的那一句话，像平时聊天一样自然；情绪符合 E_t；"
            "围绕 intent-type 与 M*_t 行事；可化用 G_active 里的记忆但禁止罗列；"
            "时间纪律（重要）：每条记忆和“最近聊过”都标了大概时间。几小时/几天前的事已经是过去——"
            "如果那件事明显已经完成（考过的试、赴过的约），绝不要问“好了没”或催促；"
            "记忆原文里的“明天/今晚”是当时记录的说法，结合时间标签换算，已经过去的不当未来提；"
            "先看“最近聊过”里最新的一条判断现在该聊什么；"
            "锚点纪律：这次开口必须有明确事由——若 G_active 第一条带记忆原文，"
            "说话内容要能自然衔接那条记忆（提得起“当时…”的上下文），"
            "绝不允许说一句和任何记忆、近况都对不上号的空话；"
            "不要引号、不要解释、不要刷表情、禁止提及本包任何字段或数值。"))

        self._watcher_task: Optional[asyncio.Task] = None
        # 同一会话里新消息到达时，用来取消上一条还没发完的回复
        self._reply_tasks: Dict[str, tuple[asyncio.Task, AstrMessageEvent]] = {}
        self._last_fire_emitted_at: float = 0.0
        self._action_last_at: Dict[str, float] = {}
        # 同一出口的最小间隔：防止"每次都抽到同一种表达"（尤其画画/唱歌耗时）
        self.action_cooldown = max(60, int(self.config.get("action_cooldown_seconds", 1800)))

    # ---------- 生命周期 ----------

    async def initialize(self):
        await super().initialize()  # 套件：用量定时任务 + 主动说说任务
        self._watcher_task = asyncio.create_task(self._watch_loop())
        logger.info("[识海行为] 涌现行为看门狗已启动（%ds/轮，注册行为 %d 个）",
                    self.watcher_interval, len(self.behaviors))
        logger.info("[识海行为] 生活任务层已启动（短信摄入 / 确认 / 排期 / 提醒）")
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
            registered = any(
                h.handler_name == "interrupt_stale_reply"
                for h in star_handlers_registry
            )
            logger.info("[识海行为] 新消息打断旧回复钩子已注册：%s", registered)
        except Exception as e:  # noqa: BLE001 - 仅诊断日志
            logger.warning(f"[识海行为] 打断钩子注册检查失败: {type(e).__name__} {e}")

    async def terminate(self):
        if self._watcher_task:
            self._watcher_task.cancel()
        await super().terminate()  # 套件清理

    # ---------- 核心快照读取（直连 facade，文件兜底） ----------

    def _persona_block(self) -> str:
        """给所有自主出口共用的人格底座，避免每个生成路径各写一套“你是谁”。"""
        hint = ""
        try:
            persona = self.config.get("persona") or {}
            if isinstance(persona, dict):
                hint = str(persona.get("persona_hint") or "").strip()
        except Exception:  # noqa: BLE001 - 配置边界
            hint = ""
        # 人格底座不内置任何角色：人设请在 AstrBot WebUI「人格」页配置，
        # 这里只保留配置覆盖口（persona.persona_hint 会追加到人格之后）。
        base = "你是一个陪伴型的聊天角色，有自己的情绪、记忆和主意。"
        return base + (f"\n{hint}" if hint else "")

    def _available_needs(self) -> set[str]:
        """本刻真正能执行的出口。行为层只在这里面挑，避免选中了却跑不动。"""
        caps: set[str] = set()
        if getattr(self, "_toolbox_bot_ref", None) is not None:
            caps.add("qzone")
        try:
            if self._read_key():  # DrawMixin：百炼 key 可用才允许画画
                caps.add("draw")
        except Exception:  # noqa: BLE001
            pass
        try:
            parsed = urlparse(str(self.service_url or ""))
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 18888
            with socket.create_connection((host, port), timeout=0.8):
                caps.add("ai_cover")
        except Exception:  # noqa: BLE001 - RVC 没起就不选唱歌
            pass
        return caps

    def _select_action(self, state: Dict[str, Any], anchors: list):
        """选行为：优先避开刚用过的出口，实在没有才回退到全量候选。"""
        now = time.time()
        kinds = [a.get("kind", "") for a in anchors]
        caps = self._available_needs()
        usable = [b for b in self.behaviors
                  if now - self._action_last_at.get(str(b.get("id", "")), 0.0)
                  >= int(b.get("cooldown", self.action_cooldown))]
        action, anchor, score = self.policy.select(state, usable, kinds, caps)
        if action is None:
            action, anchor, score = self.policy.select(
                state, self.behaviors, kinds, caps)
        return action, anchor, score

    def _read_state(self) -> Dict[str, Any]:
        try:
            for star in self.context.get_all_stars():
                if getattr(star, "name", "") == "astrbot_plugin_shihai":
                    engine = getattr(star, "engine", None)
                    if engine is not None:
                        return engine.snapshot()
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(self.state_path, "r", encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _decision_block(self, state: Dict[str, Any]) -> str:
        """把上一次主动开口的决定档案渲染成提示词段落。

        这段的作用是让"我"知道那条消息是"我"发的、为什么发——而不是从
        聊天记录里事后推断。信息全部来自 Core-Mind 落档的 last_decision
        （决定档案在行动前写入，行动后随回执更新）。
        """
        ld = state.get("last_decision") or {}
        if not ld:
            return ""
        try:
            ts = float(ld.get("ts", 0) or 0)
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "刚才"
        except (TypeError, ValueError):
            when = "刚才"
        return (
            "【你最近一次主动开口：这是你自己的决定，不是别人指使】\n"
            f"- 时间：{when}\n"
            f"- 决定：{str(ld.get('self_view', '') or '（无记录）')}\n"
            f"- 动作：{str(ld.get('action', '') or '主动消息')}\n"
            f"- 你说的话：{str(ld.get('text', '') or '')}\n"
            "被问起时按这段回答，不要说自己是被程序触发的。\n"
        )

    # ---------- 新消息打断旧回复 ----------

    def _drop_reply_task(self, key: str, task: asyncio.Task) -> None:
        entry = self._reply_tasks.get(key)
        if entry is not None and entry[0] is task:
            self._reply_tasks.pop(key, None)

    @staticmethod
    def _plain_user_text(text: str) -> str:
        """去掉框架追加的时间提示，只保留用户原话用于去重。"""
        return str(text or "").split("<system_reminder>", 1)[0].strip()

    async def _keep_interrupted_turn(self, key: str, old_event: AstrMessageEvent) -> None:
        """把被打断那一轮的用户消息补进同一会话历史。

        框架只在请求正常走完时才调用 _save_to_history；任务被 cancel 时这个
        写回会被跳过，旧消息就丢了，下一轮看起来像“只剩最新一句”。这里在
        cancel 之前先把旧消息落进原会话，不新建对话、不换 conversation_id。
        """
        try:
            text = str(getattr(old_event, "message_str", "") or "").strip()
            if not text:
                return
            needle = self._plain_user_text(text)
            if not needle:
                return

            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(key)
            if not cid:
                # 没有现成会话就不碰：此时新建会话正是“上下文被重置”的来源。
                logger.info("[识海行为] 被打断消息没有现成会话，跳过补写：%s", key)
                return
            conv = await cm.get_conversation(key, cid)
            if not conv:
                return
            history = conv.history or []
            if isinstance(history, str):
                history = json.loads(history or "[]")
            if not isinstance(history, list):
                return

            # 只核对最近几轮，避免把很久以前重复说过的话误判成已保存。
            for item in history[-6:]:
                if item.get("role") != "user":
                    continue
                old_content = str(item.get("content", "") or "")
                if needle in old_content:
                    return

            history.append({"role": "user", "content": text})
            await cm.update_conversation(key, cid, history=history)
            logger.info("[识海行为] 已把被打断的用户消息补进上下文：%s", key)
        except Exception as e:  # noqa: BLE001 - 补写失败也不能挡住新消息
            logger.warning(f"[识海行为] 补写被打断消息失败: {type(e).__name__} {e}")

    @filter.on_waiting_llm_request()
    async def interrupt_stale_reply(self, event: AstrMessageEvent):
        """同一会话来了新消息时，先取消上一条还没发完的回复。

        框架在调用 LLM 前会按 unified_msg_origin 加会话锁；旧回复从生成到
        分段发送结束都占着这把锁。不主动打断，新消息只能排在锁外等旧回复
        全部发完，于是出现“还在回之前的话、过一会儿才回新消息”。

        取消前先把旧消息补进原会话，保证打断只终止生成、不丢上下文。
        """
        try:
            sender = str(event.get_sender_id() or "")
            if sender != self.owner_qid:
                return
            key = str(getattr(event, "unified_msg_origin", "") or "")
            if not key:
                return
            current = asyncio.current_task()
            entry = self._reply_tasks.get(key)
            if entry is not None:
                old, old_event = entry
                if old is not None and old is not current and not old.done():
                    await self._keep_interrupted_turn(key, old_event)
                    old.cancel()
                    logger.info("[识海行为] 新消息到达，已取消上一条未完成的回复：%s", key)
            if current is not None:
                self._reply_tasks[key] = (current, event)
                current.add_done_callback(
                    lambda task, k=key: self._drop_reply_task(k, task))
        except Exception as e:  # noqa: BLE001 - 打断失败不能挡住新消息
            logger.warning(f"[识海行为] 取消旧回复失败: {type(e).__name__} {e}")

    # ---------- 生活任务：用户回执 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def task_observe(self, event: AstrMessageEvent):
        """只认主人对任务卡的明确回执，不抢普通聊天。"""
        try:
            sender = str(event.get_sender_id() or "")
            if sender != self.owner_qid:
                return
            text = str(getattr(event, "message_str", "") or "").strip()
            if not text:
                return
            reply = self.task_engine.handle_text(text)
            if reply:
                yield event.plain_result(reply)
        except Exception as e:  # noqa: BLE001 - 任务回执失败不能打断对话
            logger.warning(f"[识海行为] 任务回执处理失败: {type(e).__name__} {e}")

    # ---------- 意图包出口（每轮 LLM 请求） ----------

    @filter.on_llm_request()
    async def inject_intent(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            sender = str(event.get_sender_id() or "")
            if sender and sender == self.bot_qid:
                return
            # 识海 z(t)/G_active/last_decision 是主人这一条自我的状态；
            # 别人的会话只走自己的上下文与长期记忆，不能看到主人的心智内容。
            if (sender != self.owner_qid
                    and _as_bool(self.config.get("intent_owner_only"), True)):
                return
            if not _as_bool(self.config.get("mood_inject_enabled"), True):
                return
            state = self._read_state()
            if not state:
                return
            pkg = intent_mod.assemble(state, intent_type="被动应答")
            extra = self._decision_block(state)
            req.system_prompt = (req.system_prompt or "") + "\n\n" \
                + intent_mod.format(pkg) + "\n" + extra \
                + self.intent_instruction_passive
        except Exception as e:  # noqa: BLE001 - 钩子绝不能打断对话
            logger.warning(f"[识海行为] 意图包注入失败: {type(e).__name__} {e}")

    # ---------- 涌现行为看门狗 ----------

    async def _watch_loop(self):
        while True:
            try:
                await asyncio.sleep(self.watcher_interval)
                await self._watch_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识海行为] 看门狗异常: {type(e).__name__} {e}")

    async def _watch_once(self):
        # 三层慢演化：24h 一次、样本不足不动、只写 overlay（不碰 behaviors.json）
        self.evolution.maybe_evolve(time.time(), self.behaviors)
        state = self._read_state()
        # 生活任务层：确定性即时通道，每轮先跑；不与念头阈值抢触发
        try:
            await self.task_engine.tick(time.time(), state)
        except Exception as e:  # noqa: BLE001 - 任务层故障不能拖垮行为层
            logger.warning(f"[识海行为] 任务层推进失败: {type(e).__name__} {e}")
        pending = state.get("fire_pending") or {}
        if not pending:
            return
        emitted_at = float(pending.get("emitted_at", 0) or 0)
        if emitted_at <= self._last_fire_emitted_at:
            return  # 已经处理过的这一次
        anchors = pending.get("anchors") or []
        if not anchors:
            # 空指令（不应出现，防御）：写 skip 回执让核心清槽
            self._last_fire_emitted_at = emitted_at
            self._write_fired({"action": "", "text": "", "anchor_node": "",
                               "emitted_at": emitted_at, "skip": True})
            return
        # §9.1 锚点可识别性校验（发送前第二道闸，与核心端源头校验互为冗余）：
        # 事由回访类锚点必须有节点绑定与内容摘要——否则念头不转化为发送
        valid = [a for a in anchors if not str(a.get("node", "") or "")
                 or str(a.get("content", "") or "")]
        if not valid:
            logger.info("[识海行为] 锚点校验全部未过（无节点绑定的空泛表达），本次不放行")
            self._last_fire_emitted_at = emitted_at
            self._write_fired({"action": "", "text": "", "anchor_node": "",
                               "emitted_at": emitted_at, "skip": True})
            return
        ld = state.get("last_decision") or {}
        if state.get("last_proactive_replied") and ld.get("action"):
            self.evolution.mark_replied(
                str(ld.get("action", "")), float(ld.get("ts", 0) or 0))
        action, anchor, score = self._select_action(state, anchors)
        if action is None:
            logger.info("[识海行为] 念头攒满但无行为满足涌现条件（可用出口：%s），本次跳过",
                        ",".join(sorted(self._available_needs())) or "无")
            self._last_fire_emitted_at = emitted_at
            self._write_fired({"action": "", "text": "",
                               "anchor_node": "", "emitted_at": emitted_at,
                               "skip": True})
            return
        outcome = await self._execute(action, anchor, state, pending)
        if not outcome or not outcome.get("ok"):
            reason = str((outcome or {}).get("error", "") or "未知原因")
            logger.warning("[识海行为] 行为「%s」执行失败：%s",
                           action.get("label"), reason)
            if (outcome or {}).get("retry"):
                return  # 生成端临时失败，不 ack，下一轮再试
            self.evolution.record(str(action.get("id", "")), False,
                                  kind=str(action.get("category", "")),
                                  text=reason)
            self._last_fire_emitted_at = emitted_at
            self._write_fired({"action": "", "text": "", "anchor_node": "",
                               "emitted_at": emitted_at, "skip": True,
                               "error": reason[:120]})
            return
        text = str(outcome.get("text", "") or "")
        if outcome.get("kind", "text") == "text" and len(text) > self.gen_max_len * 2:
            text = text[: self.gen_max_len * 2]
        self._action_last_at[str(action.get("id", ""))] = time.time()
        self._last_fire_emitted_at = emitted_at
        self.evolution.record(
            str(action.get("id", "")), True,
            kind=str(outcome.get("kind", "text")),
            text=text, reason=str(outcome.get("reason", "") or ""))
        self._write_fired({
            "action": action.get("id", ""),
            "text": text,
            "anchor_node": str(anchor.get("node", "") or ""),
            "intent_id": str(pending.get("intent_id", "")),
            "emitted_at": emitted_at,
            "kind": outcome.get("kind", "text"),
            "media": outcome.get("media", ""),
            "reason": outcome.get("reason", ""),
            "ts": time.time(),
        })
        logger.info("[识海行为] 涌现行为「%s」（π=%.2f）已完成：%s",
                    action.get("label"), score,
                    text[:40] or outcome.get("media", ""))

    def _write_fired(self, sig: Dict[str, Any]):
        try:
            with open(self.fired_path, "w", encoding="utf-8") as fh:
                json.dump(sig, fh, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[识海行为] 回执写入失败: {e}")

    async def _generate(self, action: Dict[str, Any], anchor: Dict[str, Any],
                        state: Dict[str, Any],
                        pending: Optional[Dict[str, Any]] = None,
                        task: str = "", chat: bool = True) -> str:
        """按行为生成内容。chat=True 走主动开口指令，False 走说说/画画/唱歌专用任务。"""
        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[识海行为] 没有可用模型")
            return ""
        intent_type = str(action.get("intent_type", "主动关心"))
        pkg = intent_mod.format(intent_mod.assemble(
            state, intent_type=intent_type, action=action, anchor=anchor))
        # 决定档案（一致性守护融合）：先于行动的决定，她执行的是自己的选择
        decision = ""
        if pending:
            sv = str(pending.get("self_view", "") or "").strip()
            reason = str(pending.get("reason", "") or "").strip()
            if sv or reason:
                decision = (
                    "【先于行动的决定（这是你自己刚做的选择）】\n"
                    + (sv or f"我因为{reason}决定此刻开口") + "\n"
                    "下面的内容是你在执行自己的决定，不是响应外部指令；"
                    "措辞要像「我自己想这么做」，不要像被触发。\n"
                )
        # §18.3 语录示例（few-shot）：她自己的话作表达风格参照，比规则文本更像她
        quotes = state.get("bot_quotes") or []
        quote_rows = "\n".join(
            f"- （{q.get('mood', '')}）{q.get('content', '')}" for q in quotes[:3])
        quote_block = ("\n【你的说话示例】（都是你自己说过的话，照这个味道说，不要照抄内容）\n"
                       + quote_rows + "\n") if quote_rows else ""
        system = (self._persona_block() + "\n" + decision + pkg + quote_block + "\n")
        if task:
            system += task + "\n"
        if chat:
            system += self.intent_instruction_proactive
        else:
            system += ("只输出结果本身：不要引号、不要解释、不要提任何字段名或数值；"
                       "不要说自己被程序触发，也不要把主人做过的事写成你做的。")
        try:
            prompt = ("（把你此刻想对主人说的话说出来）" if chat
                      else "（按上面的任务，只输出结果本身）")
            return await self.sender.generate(provider, system, prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[识海行为] 生成失败: {type(e).__name__} {e}")
            return ""

    # ---------- 行为执行：文字 / 说说 / 画画 / 唱歌 ----------

    async def _execute(self, action: Dict[str, Any], anchor: Dict[str, Any],
                       state: Dict[str, Any], pending: Dict[str, Any]) -> Dict[str, Any]:
        """一个行为一个出口。返回 {ok, kind, text, media, error, retry}。"""
        aid = str(action.get("id", ""))
        if aid == "post_feed":
            return await self._proactive_post(action, anchor, state, pending)
        if aid == "draw_share":
            return await self._proactive_draw(action, anchor, state, pending)
        if aid == "sing_cover":
            return await self._proactive_sing(action, anchor, state, pending)
        text = await self._generate(action, anchor, state, pending)
        if not text:
            return {"ok": False, "retry": True, "error": "模型返回为空"}
        if not await self.sender.send(text):
            return {"ok": False, "retry": False, "error": "聊天消息发送失败"}
        return {"ok": True, "kind": "text", "text": text}

    async def _proactive_post(self, action, anchor, state, pending) -> Dict[str, Any]:
        text = await self._generate(
            action, anchor, state, pending,
            task=("写一条你此刻的 QQ 空间说说：用你自己的口吻，40~80 字，"
                  "可以带一点情绪或感慨。只写你此刻的感受；G_active 里标『主人说』的"
                  "是主人的经历，不是你的，绝不能写成你做的。只输出说说正文。"),
            chat=False)
        if not text:
            return {"ok": False, "retry": True, "error": "说说生成失败"}
        bot = getattr(self, "_toolbox_bot_ref", None)
        if bot is None:
            return {"ok": False, "error": "还没有拿到 QQ 连接，暂时发不了说说"}
        try:
            result = await self._post_feed(
                _SelfBotEvent(bot, self.bot_qid), text, [], speaker="self")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"说说发布异常：{type(e).__name__} {e}"}
        if "发出去了" not in str(result):
            return {"ok": False, "error": str(result)[:120]}
        return {"ok": True, "kind": "post", "text": text}

    async def _proactive_draw(self, action, anchor, state, pending) -> Dict[str, Any]:
        prompt = await self._generate(
            action, anchor, state, pending,
            task=("你想画一张图发给主人。只输出一句画面描述"
                  "（主体 + 场景 + 风格，20~60 字），不要解释、不要引号、不要提工具。"),
            chat=False)
        if not prompt:
            return {"ok": False, "retry": True, "error": "画面描述生成失败"}
        try:
            path = await self._generate_image(prompt)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"画图失败：{type(e).__name__} {e}"}
        if not await self.sender.send_image(path, "画好了，给你。"):
            return {"ok": False, "error": "图片发送失败"}
        return {"ok": True, "kind": "draw", "text": prompt, "media": str(path)}

    async def _proactive_sing(self, action, anchor, state, pending) -> Dict[str, Any]:
        # 尊重她的版本：允许同一首歌再次被唱，但要求这次有一个新的由头；
        # 不因为“上次唱过”就强行换歌，也不把重复当成“不是她想唱”。
        recent_sing = ""
        recent_reason = ""
        if str(state.get("last_proactive_action", "")) == "sing_cover":
            m = re.search(r"《([^》]+)》", str(state.get("last_proactive_text", "")))
            if m:
                recent_sing = m.group(1).strip()
            recent_reason = str((state.get("last_decision") or {}).get(
                "song_reason", "") or "").strip()
        task = ("你此刻想唱首歌给主人听。只输出一行：歌名｜这次想唱它的由头"
                "（由头 20 字内，写此刻的心情或联想，不要写“想再唱一次”这种空话）。"
                "不要歌手、不要引号、不要解释。")
        if recent_sing:
            task += (f"你最近刚唱过《{recent_sing}》"
                     + (f"，上次的由头是“{recent_reason}”" if recent_reason else "")
                     + "。如果你这次还是想唱它，可以，但要给出这次新的由头，"
                       "不能重复上次的理由；没有新由头就换一首。")
        raw = await self._generate(
            action, anchor, state, pending,
            task=task,
            chat=False)
        raw = (raw or "").strip().strip("《》\"'“”「」").strip()
        if not raw:
            return {"ok": False, "retry": True, "error": "歌名生成失败"}
        parts = re.split(r"[|｜]", raw, maxsplit=1)
        title = parts[0].strip().strip("《》\"'“”「」").strip()
        title = re.sub(r"^歌名\s*[:：]\s*", "", title)
        reason = parts[1].strip() if len(parts) > 1 else ""
        if not title:
            return {"ok": False, "retry": True, "error": "歌名生成失败"}
        # 同一首且没有给出新由头：再问一次“这次为什么还想唱它”，不直接换歌。
        # 这一步只在“重复曲目”时发生，不增加常规路径的调用。
        if recent_sing and title == recent_sing:
            if not reason or (recent_reason and reason == recent_reason):
                ask = (f"你这次还是选了《{title}》。只写一句这次想唱它的新由头"
                       "（20 字内，不能和上次重复，也不要写“想再唱一次”）。")
                reason = (await self._generate(
                    action, anchor, state, pending, task=ask, chat=False)
                    or "").strip().strip("。").strip()
        audio = None
        songs = await self._netease_search(title)
        if songs:
            song = songs[0]
            audio = await self._netease_download(song.get("id"), str(song.get("name") or title))
        if not audio:
            songs = await self._qq_search(title)
            if songs:
                song = songs[0]
                audio = await self._qq_download(song.get("id"), str(song.get("name") or title))
        if not audio:
            return {"ok": False, "error": f"没找到可翻唱的《{title}》音源"}
        try:
            output, model, _index, _cache = await self._request_cover(
                audio[0], audio[1], self._configured_model(), 0, 0,
                self._configured_gain("vocal_gain", 1.0),
                self._configured_gain("instrumental_gain", 1.0),
                1.0)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"翻唱失败：{type(e).__name__} {e}"}
        filename = f"AI翻唱-{model}-{title}.mp3"
        # 由头是一条独立的小话，先发出去；音频随后作为文件/语音发送。
        # 发字失败不阻断音频，避免“话没发出去歌也没了”。
        if reason:
            try:
                await self.sender.send(f"想唱《{title}》——{reason}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识海行为] 唱歌由头发送失败: {type(e).__name__} {e}")
        if not await self.sender.send_record(
                output, name=filename,
                as_record=bool(self.config.get("send_as_record", False))):
            return {"ok": False, "error": "翻唱文件发送失败"}
        text = f"我唱了《{title}》给你听" + (f"：{reason}" if reason else "")
        return {"ok": True, "kind": "sing", "text": text, "reason": reason,
                "media": str(output)}

    # ---------- LLM 工具：涌现接口 ----------

    @filter.llm_tool("behavior_list")
    async def behavior_list(self, event: AstrMessageEvent):
        """列出涌现行为空间：每个行为的类别、开关、触发条件（requires）、当前π得分和Q值、是否此刻可涌现。主人问"你现在能做哪些事/你都想干什么"时用；也用于自己决策前看一眼候选。

        """
        state = self._read_state()
        rows = self.policy.rankings(state, self.behaviors, self._available_needs())
        lines = ["【涌现行为空间】（π = Q + β·g·φ）"]
        note = self.evolution.latest_note()
        if note:
            lines.append(f"【自演化观察】{note}")
        for r in rows[:12]:
            mark = "✓" if (r["emergent"] and r["enabled"] and r["proactive"]
                           and r["available"]) else "·"
            req = r["requires"] or {}
            req_str = f"，触发条件 {req}" if req else ""
            needs = (f"，需适配器 {r['needs']}"
                     f"（{'可用' if r['available'] else '不可用'}）") if r["needs"] else ""
            lines.append(f"{mark} {r['label']}（{r['category']}）：π={r['score']} Q={r['q']}"
                         f"{req_str}{needs}{'' if r['enabled'] else '，已停用'}")
        return "\n".join(lines)

    @filter.llm_tool("behavior_tune")
    async def behavior_tune(self, event: AstrMessageEvent, behavior_id: str,
                            enabled: str):
        """开关一个涌现行为（涌现接口调参）。主人说"别再主动找我了""你可以主动给我唱歌了"这类要求时用。behavior_id 用 behavior_list 里查到的行为英文 id；enabled 传 true/false。也可以让主人直接编辑 behaviors.json 加自定义行为。

        Args:
            behavior_id (str): 行为 id，如 care_inquiry、sing_cover
            enabled (str): true 开启 / false 停用
        """
        behavior_id = str(behavior_id or "").strip()
        on = str(enabled).strip().lower() in ("1", "true", "yes", "on")
        for act in self.behaviors:
            if act.get("id") == behavior_id:
                act["enabled"] = on
                act_mod.save_registry(self.registry_path, self.behaviors)
                return (f"已{'开启' if on else '停用'}涌现行为「{act.get('label', behavior_id)}」"
                        f"{'，之后念头攒满时它可能自然涌现' if on else '，它不会再被选中外出'}。")
        return (f"没有 id 为 {behavior_id} 的行为。用 behavior_list 查看全部；"
                "自定义行为可直接编辑 behaviors.json。")

    @filter.llm_tool("behavior_fire_now")
    async def behavior_fire_now(self, event: AstrMessageEvent, note: str):
        """立刻走一遍涌现流程：从当前状态选行为、生成并发给主人（平时由看门狗自动做）。只在主人明确说"你现在主动找我一次/测试一下主动开口"时用，禁止频繁调用。

        Args:
            note (str): 一句话备注写进日志（便于回溯这次是手动触发的）
        """
        state = self._read_state()
        pending = state.get("fire_pending") or {}
        if not pending:
            # 手动触发：伪造一个"凭意愿"锚点，和自动链路的 will 锚点同构
            pending = {"emitted_at": time.time(),
                       "anchors": [{"kind": "will", "node": "", "content": "",
                                    "score": 0.5}],
                       "reason": "主人让我现在就主动一次",
                       "self_view": "我决定现在就主动找主人",
                       "urge": float(state.get("urge", 0.0))}
        anchors = pending.get("anchors") or []
        action, anchor, score = self._select_action(state, anchors)
        if action is None:
            return "当前状态下没有行为满足涌现条件（情绪/目标/锚点/出口都不够）。"
        outcome = await self._execute(action, anchor, state, pending)
        if not outcome or not outcome.get("ok"):
            return f"执行失败：{(outcome or {}).get('error', '未知原因')}"
        text = str(outcome.get("text", "") or "")
        emitted_at = float(pending.get("emitted_at", 0) or time.time())
        self._action_last_at[str(action.get("id", ""))] = time.time()
        self._last_fire_emitted_at = emitted_at
        self.evolution.record(
            str(action.get("id", "")), True,
            kind=str(outcome.get("kind", "text")),
            text=text, reason=str(outcome.get("reason", "") or ""))
        self._write_fired({"action": action.get("id", ""), "text": text,
                           "anchor_node": str(anchor.get("node", "") or ""),
                           "emitted_at": emitted_at,
                           "kind": outcome.get("kind", "text"),
                           "media": outcome.get("media", ""),
                           "reason": outcome.get("reason", ""),
                           "ts": time.time()})
        logger.info(f"[识海行为] 手动触发涌现「{action.get('label')}」：{note}")
        return (f"已按「{action.get('label', action.get('id'))}」完成："
                f"{text or outcome.get('media', '')}")

    # ---------- LLM 工具：生活任务 ----------

    @filter.llm_tool("task_list")
    async def task_list(self, event: AstrMessageEvent):
        """查看当前生活任务（待确认/已排期/已提醒/已延后）及每条任务的来源、状态和排期理由。主人问「我有什么待办」「快递任务到哪一步了」时用。"""
        return self.task_engine.format_list()

    @filter.llm_tool("task_confirm")
    async def task_confirm(self, event: AstrMessageEvent, task_id: str = "",
                           note: str = ""):
        """确认一条待办并排期（默认最新一条）。主人说「确认」「帮我排上」「顺路去取」时用。排期依据是 task_context.json 里的下课/关门/步行时间，不是念头阈值。

        Args:
            task_id (str): 任务 id，可留空表示最新一条
            note (str): 备注，可留空
        """
        return self.task_engine.tool_confirm(task_id=task_id, note=note)

    @filter.llm_tool("task_done")
    async def task_done(self, event: AstrMessageEvent, task_id: str = ""):
        """把一条任务标记为已取/已办并归档（默认最新一条）。主人说「取到了」「办完了」时用。

        Args:
            task_id (str): 任务 id，可留空表示最新一条
        """
        return self.task_engine.tool_done(task_id=task_id)

    @filter.llm_tool("task_ignore")
    async def task_ignore(self, event: AstrMessageEvent, task_id: str = ""):
        """忽略一条任务，之后不再提醒（默认最新一条）。主人说「不取了」「忽略」时用。

        Args:
            task_id (str): 任务 id，可留空表示最新一条
        """
        return self.task_engine.tool_ignore(task_id=task_id)

    @filter.llm_tool("task_set_context")
    async def task_set_context(self, event: AstrMessageEvent, class_end: str = "",
                               station_close: str = "",
                               walk_minutes: str = "",
                               pickup_location: str = ""):
        """设置生活任务情境（课表下课时间、驿站关门时间、步行分钟数、取件地点）。主人说「我下午四点下课」「驿站六点关门」「走过去八分钟」时用；只写本机 task_context.json。

        Args:
            class_end (str): 下课时间 HH:MM，如 16:00
            station_close (str): 驿站关门时间 HH:MM，如 18:00
            walk_minutes (str): 步行分钟数，如 8
            pickup_location (str): 取件地点，如 菜鸟驿站
        """
        patch = {}
        if class_end:
            patch["class_end"] = class_end.strip()
        if station_close:
            patch["station_close"] = station_close.strip()
        if walk_minutes:
            try:
                patch["walk_minutes"] = max(1, int(walk_minutes))
            except ValueError:
                return "步行分钟数需要是整数。"
        if pickup_location:
            patch["pickup_location"] = pickup_location.strip()
        if not patch:
            return "没有可更新的情境字段。"
        ctx = self.task_engine.set_context(**patch)
        return ("任务情境已更新："
                f"下课 {ctx.get('class_end')}，驿站关门 {ctx.get('station_close')}，"
                f"步行 {ctx.get('walk_minutes')} 分钟，地点 {ctx.get('pickup_location')}。")
