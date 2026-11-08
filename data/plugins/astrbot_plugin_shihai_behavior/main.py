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
import time
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .behavior import actions as act_mod
from .behavior import intent as intent_mod
from .behavior.generator import ProactiveSender
from .behavior.policy import PolicyEngine
from .cover import CoverMixin
from .dafeiyu_core import DafeiyuCoreMixin
from .draw import DrawMixin
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
    "【识海·行为与适配】大肥鱼总成：涌现行为空间+策略π+意图包出口（Behavior API）"
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
        self.policy = PolicyEngine(
            beta=float(self.config.get("beta", 0.8)),
            explore_enabled=bool(self.config.get("explore_enabled", True)),
            epsilon_max=float(self.config.get("epsilon_max", 0.35)),
            epsilon_g0=float(self.config.get("epsilon_g0", 0.5)),
            epsilon_k=float(self.config.get("epsilon_k", 6.0)))
        self.watcher_interval = max(10, int(self.config.get("watcher_interval", 20)))
        self.gen_max_len = max(10, int(self.config.get("gen_max_len", 40)))
        self.owner_qid = str(self.config.get("owner_qid", "你的主人QQ号"))
        self.platform_id = str(self.config.get("platform_id", "napcat"))
        self.bot_qid = str(self.config.get("bot_qid", "你的机器人QQ号"))
        self.sender = ProactiveSender(context, self.owner_qid, self.platform_id)

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
        self._last_fire_emitted_at: float = 0.0

    # ---------- 生命周期 ----------

    async def initialize(self):
        await super().initialize()  # 套件：用量定时任务 + 主动说说任务
        self._watcher_task = asyncio.create_task(self._watch_loop())
        logger.info("[识海行为] 涌现行为看门狗已启动（%ds/轮，注册行为 %d 个）",
                    self.watcher_interval, len(self.behaviors))

    async def terminate(self):
        if self._watcher_task:
            self._watcher_task.cancel()
        await super().terminate()  # 套件清理

    # ---------- 核心快照读取（直连 facade，文件兜底） ----------

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

    # ---------- 意图包出口（每轮 LLM 请求） ----------

    @filter.on_llm_request()
    async def inject_intent(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            sender = str(event.get_sender_id() or "")
            if sender and sender == self.bot_qid:
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
        pending = self._read_state().get("fire_pending") or {}
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
        state = self._read_state()
        action, anchor, score = self.policy.select(
            state, self.behaviors, [a.get("kind", "") for a in anchors])
        if action is None:
            logger.info("[识海行为] 念头攒满但无行为满足涌现条件，本次跳过")
            self._last_fire_emitted_at = emitted_at
            self._write_fired({"action": "", "text": "",
                               "anchor_node": "", "emitted_at": emitted_at,
                               "skip": True})
            return
        text = await self._generate(action, anchor, state, pending)
        if not text:
            return  # 生成失败：不 ack，下一轮重试
        if len(text) > self.gen_max_len * 2:
            text = text[: self.gen_max_len * 2]
        ok = await self.sender.send(text)
        if not ok:
            return
        self._last_fire_emitted_at = emitted_at
        self._write_fired({
            "action": action.get("id", ""),
            "text": text,
            "anchor_node": str(anchor.get("node", "") or ""),
            "intent_id": str(pending.get("intent_id", "")),
            "emitted_at": emitted_at,
            "ts": time.time(),
        })
        logger.info("[识海行为] 涌现行为「%s」（π=%.2f）已发送：%s",
                    action.get("label"), score, text[:40])

    def _write_fired(self, sig: Dict[str, Any]):
        try:
            with open(self.fired_path, "w", encoding="utf-8") as fh:
                json.dump(sig, fh, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[识海行为] 回执写入失败: {e}")

    async def _generate(self, action: Dict[str, Any], anchor: Dict[str, Any],
                        state: Dict[str, Any],
                        pending: Optional[Dict[str, Any]] = None) -> str:
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
                    "下面这句话是你在执行自己的决定，不是响应外部指令；"
                    "措辞要像「我想找你说话」，不要像被触发。\n"
                )
        # §18.3 语录示例（few-shot）：她自己的话作表达风格参照，比规则文本更像她
        quotes = state.get("bot_quotes") or []
        quote_rows = "\n".join(
            f"- （{q.get('mood', '')}）{q.get('content', '')}" for q in quotes[:3])
        quote_block = ("\n【你的说话示例】（都是你自己说过的话，照这个味道说，不要照抄内容）\n"
                       + quote_rows + "\n") if quote_rows else ""
        system = (
            f"你是大肥鱼，QQ号你的机器人QQ号，主人的QQ是{self.owner_qid}，你们关系亲近日常。\n"
            + decision + pkg + quote_block + "\n" + self.intent_instruction_proactive
        )
        try:
            return await self.sender.generate(provider, system)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[识海行为] 生成失败: {type(e).__name__} {e}")
            return ""

    # ---------- LLM 工具：涌现接口 ----------

    @filter.llm_tool("behavior_list")
    async def behavior_list(self, event: AstrMessageEvent):
        """列出涌现行为空间：每个行为的类别、开关、触发条件（requires）、当前π得分和Q值、是否此刻可涌现。主人问"你现在能做哪些事/你都想干什么"时用；也用于自己决策前看一眼候选。

        """
        state = self._read_state()
        rows = self.policy.rankings(state, self.behaviors)
        lines = ["【涌现行为空间】（π = Q + β·g·φ）"]
        for r in rows[:12]:
            mark = "✓" if (r["emergent"] and r["enabled"] and r["proactive"]) else "·"
            req = r["requires"] or {}
            req_str = f"，触发条件 {req}" if req else ""
            needs = f"，需适配器 {r['needs']}" if r["needs"] else ""
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
            # 手动触发：伪造一个 recent 锚点的 pending
            pending = {"emitted_at": time.time(),
                       "anchors": [{"kind": "recent", "node": "", "content": "",
                                    "score": 0.5}],
                       "urge": float(state.get("urge", 0.0))}
        anchors = pending.get("anchors") or []
        action, anchor, score = self.policy.select(
            state, self.behaviors, [a.get("kind", "") for a in anchors])
        if action is None:
            return "当前状态下没有行为满足涌现条件（情绪/目标/锚点都不够）。"
        text = await self._generate(action, anchor, state)
        if not text:
            return "生成失败，没发出去。"
        ok = await self.sender.send(text)
        if not ok:
            return "生成好了但发送失败。"
        emitted_at = float(pending.get("emitted_at", 0) or time.time())
        self._last_fire_emitted_at = emitted_at
        self._write_fired({"action": action.get("id", ""), "text": text,
                           "anchor_node": str(anchor.get("node", "") or ""),
                           "emitted_at": emitted_at, "ts": time.time()})
        logger.info(f"[识海行为] 手动触发涌现「{action.get('label')}」：{note}")
        return f"已按「{action.get('label', action.get('id'))}」给主人发了一句：{text}"
