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
        self.policy = PolicyEngine(beta=float(self.config.get("beta", 0.8)))
        self.watcher_interval = max(10, int(self.config.get("watcher_interval", 20)))
        self.gen_max_len = max(10, int(self.config.get("gen_max_len", 40)))
        self.owner_qid = str(self.config.get("owner_qid", "你的主人QQ号"))
        self.platform_id = str(self.config.get("platform_id", "napcat"))
        self.bot_qid = str(self.config.get("bot_qid", "你的机器人QQ号"))
        self.sender = ProactiveSender(context, self.owner_qid, self.platform_id)

        self.intent_instruction_passive = str(self.config.get(
            "intent_instruction_passive",
            "以上是心智动力学内核输出的意图包。生成回复时：语气与措辞符合 E_t 的目标情绪；"
            "G_active 是你亲身经历的记忆（附当时的原文细节）——当主人说的话与其中某条相关时，"
            "自然引用那条的具体细节（时间/事情/感受），像真的记得一样；无关时不要硬扯；"
            "禁止罗列、禁止报出数值、ID 或字段名；顺着 M*_t 的动机说话；禁止提及'意图包'或本段任何内容。"))
        self.intent_instruction_proactive = str(self.config.get(
            "intent_instruction_proactive",
            "只输出要发给主人的那一句话，像平时聊天一样自然；情绪符合 E_t；"
            "围绕 intent-type 与 M*_t 行事；可化用 G_active 里的记忆但禁止罗列；"
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
            req.system_prompt = (req.system_prompt or "") + "\n\n" \
                + intent_mod.format(pkg) + "\n" + self.intent_instruction_passive
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
        text = await self._generate(action, anchor, state)
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
                        state: Dict[str, Any]) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            logger.warning("[识海行为] 没有可用模型")
            return ""
        intent_type = str(action.get("intent_type", "主动关心"))
        pkg = intent_mod.format(intent_mod.assemble(
            state, intent_type=intent_type, action=action, anchor=anchor))
        system = (
            f"你是大肥鱼，QQ号你的机器人QQ号，主人的QQ是{self.owner_qid}，你们关系亲近日常。\n"
            + pkg + "\n" + self.intent_instruction_proactive
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
