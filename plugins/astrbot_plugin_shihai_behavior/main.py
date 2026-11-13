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
import hashlib
import json
import os
import random
import re
import socket
import time
from datetime import datetime, timedelta
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
from .behavior.schedule import ScheduleStore
from .cover import CoverMixin
from .dafeiyu_core import DafeiyuCoreMixin
from .draw import DrawMixin
from .modules import privacy_gate as privacy_mod
from .modules import utterance_mode as utterance_mod
from .modules.toolbox import _SelfBotEvent
from .suite import ToolboxSuiteBase

# 唱歌出口失败后的熔断时长：这一小时内不再把 ai_cover 当成可用出口
# （音源因版权/VIP 拿不到属于持续性故障，重试一次就够，不该每 20s 撞一次）
COVER_FAIL_COOLDOWN = 3600

PLUGIN_NAME = "astrbot_plugin_shihai_behavior"
STATE_DIR_NAME = "shihai"  # 契约：与核心共享的数据目录

# 指代消解用的对象词表：命中即记为"当前焦点"，后续「她/他/它」绑定到它。
# 只收关系称谓，不收泛词，避免把「朋友」这类高频词误当焦点。
_FOCUS_WORDS = (
    "姐姐", "妹妹", "哥哥", "弟弟", "妈妈", "爸爸", "母亲", "父亲",
    "奶奶", "爷爷", "姥姥", "姥爷", "闺女", "儿子",
    "室友", "同桌", "同学", "老师", "导员", "辅导员",
    "女朋友", "男朋友", "对象", "前任", "舍友", "同事", "老板", "学长", "学姐",
)
_FOCUS_PRONOUNS = ("她", "他", "它")
_FOCUS_TTL = 6 * 3600  # 6 小时内的指代还算"最近说的那个人"



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
        self._schedule = ScheduleStore(
            json_path=Path(__file__).parent / "schedule.json",
            fired_path=data_dir / "schedule_fired.json",
            watcher_interval=self.watcher_interval)

        # §12.5 提示词瘦身：指令只留"情绪一句、记忆引用一句、时间纪律一句、边界一句"。
        # 旧版 430 字/轮 → 新版约 160 字/轮；长解释交给意图包本身的时间标签与语料。
        self.intent_instruction_passive = str(self.config.get(
            "intent_instruction_passive",
            "（下面是内核附给你的意图包，仅你可见。）回复时：语气贴合「此刻情绪」；「记得的事/最近聊过」是你亲历的"
            "（附大概时间）——相关就自然引用里面的细节，像真的记得一样，无关别硬扯；"
            "几小时/几天前的事已是过去，用过去式说，明显已完成的别追问「好了没」；"
            "记忆原文里的「明天/今晚」按时间标签换算成实际日期，已过去的不当未来的事提；"
            "你自己发过的消息都要像记得一样自然衔接；"
            "禁止罗列、禁止报出数值或字段名、禁止提及「意图包」或本段内容。"))
        self.intent_instruction_proactive = str(self.config.get(
            "intent_instruction_proactive",
            "只输出要发给主人的那一句话，像平时聊天一样自然；情绪贴合「此刻情绪」；"
            "开口必须有明确事由——第一条记忆带原文时，说话要能自然衔接那条记忆"
            "（提得起「当时…」的上下文），不说和记忆、近况都对不上号的空话；"
            "几小时/几天前的事已是过去，明显已完成的别追问或催促；"
            "不要引号、不要解释、不要刷表情、不要提及本包字段或数值。"))

        self._watcher_task: Optional[asyncio.Task] = None
        # 同一会话里新消息到达时，用来取消上一条还没发完的回复
        self._reply_tasks: Dict[str, tuple[asyncio.Task, AstrMessageEvent]] = {}
        # 当前焦点对象：{会话串: (对象词, 记录时刻)}，只给主人会话用
        self._focus: Dict[str, tuple[str, float]] = {}
        self._last_fire_emitted_at: float = 0.0
        # 晨间简报：记住最后一次发过的日期，防同一早晨多轮重复发
        self._brief_day: str = ""
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
            logger.info("[识海行为] 上下文闭合已启用：语音自述入历史 / 指代焦点绑定 / 半截回复抢救")
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
        base = ("你叫小满（鑫鑫也叫你大肥鱼），是一只住在 QQ 里的蓝色大肥鱼，"
                "和鑫鑫关系亲近；你有自己的情绪、记忆和主意，说话元气、懒萌、不客套。"
                "称呼只用「鑫鑫」和「小满」，不要自称或被称「宠物」，也不要把鑫鑫叫成「主人」。")
        return base + (f"\n{hint}" if hint else "")

    def _available_needs(self) -> set[str]:
        """本刻真正能执行的出口。行为层只在这里面挑，避免选中了却跑不动。"""
        caps: set[str] = set()
        # dafeiyu 出口（发表情包/写日记/改签名）的工具都在本插件内，
        # 不依赖外部服务，恒可用——否则行为层永远判“不可用”
        caps.add("dafeiyu")
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
                # 出口熔断（B）：RVC 服务在，不等于这次真能唱——音源可能因版权/
                # VIP 拿不到（cover.py 的 result=104003）。最近这个出口失败过就先
                # 判不可用，让 π 回落到 proactive_chat / care_inquiry 这类不依赖
                # 外部服务的出口，而不是每次攒满念头都撞同一个死胡同。
                if (time.time() - float(getattr(self, "_cover_fail_at", 0.0) or 0.0)
                        >= COVER_FAIL_COOLDOWN):
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
        # 冷落加成：Q 表越赢越赢，会把别的出口饿死（实测 10 天里承诺跟进占
        # 67%，五条行为一次没露面）。24 小时没出场的行为每小时补 0.02，封顶
        # 0.5 —— 只补到"能参与竞争"，不硬塞。
        bonus: Dict[str, float] = {}
        for b in self.behaviors:
            bid = str(b.get("id", ""))
            if not b.get("proactive") or not b.get("enabled", True):
                continue
            gap_h = (now - self._action_last_at.get(bid, 0.0)) / 3600.0
            if gap_h > 24:
                bonus[bid] = min(0.5, 0.02 * (gap_h - 24))
        action, anchor, score = self.policy.select(
            state, usable, kinds, caps, bonus=bonus)
        if action is None:
            action, anchor, score = self.policy.select(
                state, self.behaviors, kinds, caps, bonus=bonus)
        if action is None:
            # 锚点类型把候选池锁死（只有 promise 锚点 → 只有承诺跟进可选）。
            # 这时才对"最久没露面"的行为放宽锚点，别让某一类事由彻底垄断。
            stale = [b for b in self.behaviors
                     if b.get("proactive") and b.get("enabled", True)
                     and now - self._action_last_at.get(str(b.get("id", "")), 0.0)
                     >= 24 * 3600]
            if stale:
                action, anchor, score = self.policy.select(
                    state, stale, kinds, caps, bonus=bonus, ignore_anchor=True)
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
            # §12.5：超过 72h 的决策档案不再每轮注入（省 Token），
            # 连续性由会话历史与记忆层承接；新档案由下次主动开口刷新。
            if ts and time.time() - ts > 72 * 3600:
                return ""
            when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "刚才"
        except (TypeError, ValueError):
            when = "刚才"
        return (
            "【你最近一次主动开口：这是你自己的决定，不是别人指使】\n"
            f"- {when}你以「{str(ld.get('action', '') or '主动消息')}」说："
            f"{str(ld.get('text', '') or '')}\n"
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

    # ---------- 会话历史补写：她的手/他的口，两种"她说过的"都要落盘 ----------

    @staticmethod
    def _norm_line(text: str) -> str:
        """比较用的归一化：去掉句首【语气】标注、空白和常见标点。"""
        t = re.sub(r"^\s*【[^】]*】", "", str(text or ""))
        t = re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()…—\-]+", "", t)
        return t

    def _lines_alike(self, a: str, b: str) -> bool:
        """两句是否"已经是同一条"：4 字片段重叠 ≥0.6 就算重复。"""
        if not a or not b:
            return False
        if a == b:
            return True
        g1 = self._grams4(a)
        g2 = self._grams4(b)
        if not g1 or not g2:
            return False
        return len(g1 & g2) / max(1, len(g1)) >= 0.6

    @staticmethod
    def _result_text(event: AstrMessageEvent) -> str:
        """取出这一次真正发出去的文字（Record 等非文本组件跳过）。"""
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        if not chain:
            return ""
        parts = []
        for comp in chain:
            if comp is None or comp.__class__.__name__ != "Plain":
                continue
            t = getattr(comp, "text", None)
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
        return "\n".join(parts).strip()

    async def _append_history(self, key: str, role: str, text: str,
                              dedupe_tail: int = 4) -> bool:
        """把一条消息补进当前会话历史（不新建会话、不换 conversation_id）。

        框架只在请求正常走完时写回历史；被 cancel 的那轮、以及被替换成语音
        的那轮（chain 里已经没有 Plain 文本）都不会留下 assistant 记录。
        于是"她说过的话"在下一轮就不存在——表现就是「这句我不记得自己说过」。
        """
        try:
            body = str(text or "").strip()
            if not body:
                return False
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(key)
            if not cid:
                logger.info("[识海行为] 补写历史：没有现成会话，跳过（%s）", key)
                return False
            conv = await cm.get_conversation(key, cid)
            if not conv:
                return False
            history = conv.history or []
            if isinstance(history, str):
                history = json.loads(history or "[]")
            if not isinstance(history, list):
                return False
            norm_new = self._norm_line(body)
            for item in history[-dedupe_tail:]:
                if not isinstance(item, dict) or item.get("role") != role:
                    continue
                old = item.get("content", "")
                if isinstance(old, list):
                    old = "".join(str((c or {}).get("text", ""))
                                  for c in old if isinstance(c, dict))
                if norm_new and self._lines_alike(norm_new, self._norm_line(old)):
                    return False
            history.append({"role": role,
                            "content": [{"type": "text", "text": body}]})
            await cm.update_conversation(key, cid, history=history)
            return True
        except Exception as e:  # noqa: BLE001 - 补写失败不能挡住对话
            logger.warning(f"[识海行为] 补写历史失败: {type(e).__name__} {e}")
            return False

    async def _remember_self_line(self, key: str, text: str, tag: str = "语音") -> None:
        """把"她自己发出去的那句话"记成 assistant 轮。

        语音出口会把整条 chain 换成 Record，框架因此什么都没存下来；
        下一轮她只能看到自己"发了条语音"，不知道说的是什么，就会答
        「这句我不记得自己说过」。发送成功后立刻补一条历史即可闭合。
        """
        body = str(text or "").strip()
        if not body:
            return
        marked = f"（{tag}）{body}" if tag else body
        ok = await self._append_history(key, "assistant", marked)
        if ok:
            logger.info("[识海行为] 已把%s内容记进上下文：%s", tag, body[:40])

    async def _keep_partial_reply(self, key: str, old_event: AstrMessageEvent) -> None:
        """打断前抢救旧回复已经生成的那半截文本。

        只补用户消息是不够的：她被打断时已经想好/说出了一部分，若这部分不落
        盘，下一轮她会以为自己"没说过"，用户则会觉得她"刚说的话不认账"。
        """
        try:
            joined = self._result_text(old_event)
            if not joined:
                return
            await self._append_history(key, "assistant",
                                       f"（说到一半被打断）{joined[:400]}")
        except Exception as e:  # noqa: BLE001 - 抢救失败不影响打断
            logger.warning(f"[识海行为] 半截回复补写失败: {type(e).__name__} {e}")

    def _focus_file(self, key: str) -> Optional[Path]:
        """焦点落盘路径：重启后「她是谁」还能对上号（进程重启会丢内存态）。"""
        try:
            digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
            d = Path(get_astrbot_plugin_data_path()) / STATE_DIR_NAME / "focus"
            d.mkdir(parents=True, exist_ok=True)
            return d / f"{digest}.json"
        except Exception:  # noqa: BLE001 - 落盘失败就当没有
            return None

    def _load_focus(self, key: str) -> Optional[tuple[str, float]]:
        cur = self._focus.get(key)
        if cur:
            return cur
        p = self._focus_file(key)
        if not p or not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            word = str(data.get("word") or "")
            ts = float(data.get("ts") or 0)
        except Exception:  # noqa: BLE001 - 坏文件不影响对话
            return None
        if not word:
            return None
        self._focus[key] = (word, ts)
        return self._focus[key]

    def _update_focus(self, key: str, text: str) -> Optional[str]:
        """从用户这句话里抽出"当前焦点对象"，供下一轮的「她/他」绑定。"""
        t = str(text or "")
        hit = ""
        for w in _FOCUS_WORDS:
            idx = t.rfind(w)
            if idx >= 0 and (not hit or idx > t.rfind(hit)):
                hit = w
        if hit:
            self._focus[key] = (hit, time.time())
            p = self._focus_file(key)
            if p:
                try:
                    p.write_text(json.dumps(
                        {"word": hit, "ts": time.time(), "key": key},
                        ensure_ascii=False), encoding="utf-8")
                except Exception:  # noqa: BLE001 - 落盘失败不影响本轮
                    pass
        cur = self._load_focus(key)
        if not cur:
            return None
        if time.time() - cur[1] > _FOCUS_TTL:
            self._focus.pop(key, None)
            return None
        return cur[0]

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
            needle = self._norm_line(text)
            for item in history[-6:]:
                if item.get("role") != "user":
                    continue
                old = item.get("content", "")
                if isinstance(old, list):
                    old = "".join(str((c or {}).get("text", ""))
                                  for c in old if isinstance(c, dict))
                if needle and self._lines_alike(needle, self._norm_line(str(old))):
                    return

            history.append({"role": "user",
                            "content": [{"type": "text", "text": text}]})
            await cm.update_conversation(key, cid, history=history)
            logger.info("[识海行为] 已把被打断的用户消息补进上下文：%s", key)
        except Exception as e:  # noqa: BLE001 - 补写失败也不能挡住新消息
            logger.warning(f"[识海行为] 补写被打断消息失败: {type(e).__name__} {e}")

    @filter.after_message_sent()
    async def remember_sent_line(self, event: AstrMessageEvent):
        """发送结束后，把"她这一次真正说出口的话"补进历史。

        框架的 _save_to_history 保存的是本轮请求里的 messages：语音出口的正文
        被替换成 Record、工具调用链末尾的可见回复也常常只剩 think 块，落库时
        就丢了。这里在落库之后按"实际发出去的 chain"补一条 assistant 轮，
        下一轮她才接得住自己刚说过的话。
        """
        try:
            sender = str(event.get_sender_id() or "")
            key = str(getattr(event, "unified_msg_origin", "") or "")
            if not key or (sender and sender == self.bot_qid):
                return
            # 这里曾经只对 owner 会话补记（sender != owner_qid 就 return）。
            # 但语音出口的正文被换成 Record 后框架不落库，限制一加，非 owner 会话的
            # assistant 轮就整段丢失：ctx_msgs 停止增长，她下一轮不知道自己刚说过什么。
            # 补记是"按实际发出的 chain 记一条 assistant 轮"，对所有会话都成立，
            # 故不再按 owner 区分。
            voices = list(event.get_extra("_shihai_voice_text") or [])
            if voices:
                event.set_extra("_shihai_voice_text", [])
                for line in voices:
                    await self._remember_self_line(key, line)
                    await self._archive_transcript(event, key, line, kind="voice")
                return
            text = self._result_text(event)
            if text:
                await self._remember_self_line(key, text, tag="")
                await self._archive_transcript(event, key, text, kind="text")
        except Exception as e:  # noqa: BLE001 - 补记失败不能影响出话
            logger.warning(f"[识海行为] 发送后补记失败: {type(e).__name__} {e}")

    async def _archive_transcript(self, event: AstrMessageEvent, key: str,
                                 text: str, kind: str = "text") -> None:
        """把小满实际发出的话逐字归档到 transcript.db（方案 A）。

        不传 msg_id：此处 event.message_obj 是用户消息，其 id 已被用户侧
        归档占用；assistant 侧靠 turn_id 关联同一轮，允许一轮多条。
        """
        try:
            body = str(text or "").strip()
            if not body:
                return
            turn_id = event.get_extra("_shihai_turn_id") or None
            group_id = getattr(event.message_obj, "group_id", None)
            await asyncio.to_thread(
                self._transcript.append,
                role="assistant", content=body, ts=time.time(), origin=key,
                sender_id=str(self.bot_qid or ""), sender_name="小满",
                group_id=str(group_id) if group_id else None,
                kind=kind, turn_id=turn_id)
        except Exception as e:  # noqa: BLE001 - 归档失败不能影响出话
            logger.warning(f"[识海行为] 逐字归档回复失败: {type(e).__name__} {e}")

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
                    await self._keep_partial_reply(key, old_event)
                    old.cancel()
                    logger.info("[识海行为] 新消息到达，已取消上一条未完成的回复：%s", key)
                else:
                    logger.info("[识海行为] 打断钩子触发但旧回复已结束：%s", key)
            else:
                logger.info("[识海行为] 打断钩子首次记录本会话：%s", key)
            if current is not None:
                self._reply_tasks[key] = (current, event)
                current.add_done_callback(
                    lambda task, k=key: self._drop_reply_task(k, task))
        except Exception as e:  # noqa: BLE001 - 打断失败不能挡住新消息
            logger.warning(f"[识海行为] 取消旧回复失败: {type(e).__name__} {e}")

    # ---------- 空文本事件（戳一戳）----------
    # 戳一戳在 aiocqhttp 里被转成"只有 Poke 组件、没有文字"的消息事件：
    # 它不触发 LLM，所以模型那边根本收不到，表现就是"她不理人"。
    # 这里显式接住它，并只在熟人档以内回应（陌生号戳她一律不理）。
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def poke_notice(self, event: AstrMessageEvent):
        try:
            sender = str(event.get_sender_id() or "")
            text = str(getattr(event, "message_str", "") or "").strip()
            comps: list[str] = []
            try:
                for c in (getattr(getattr(event, "message_obj", None), "message", None) or []):
                    comps.append(type(c).__name__)
            except Exception:  # noqa: BLE001
                pass
            poked = "Poke" in comps
            if not poked:
                if not text:
                    logger.info("[inbox] 空文本事件 sender=%s comps=%s（不进 LLM）",
                                sender, ",".join(comps) or "-")
                return
            if not sender or sender == str(self.bot_qid):
                return
            known = (sender == str(self.owner_qid)
                     or ("qq:" + sender) in set(getattr(self, "_known_ids", set())))
            logger.info("[inbox] 戳一戳 sender=%s 熟人=%s", sender, known)
            if not known:
                return  # 陌生号：不理，与 restricted 档一致
            line = random.choice(
                ("干嘛呀，戳我。", "在呢在呢，别戳了~", "戳一下就想让我说话？", "嗯？戳我做什么。")
                if sender == str(self.owner_qid)
                else ("嗯？有事就说呀。", "戳我做什么，我又不会疼。", "在的，怎么了？"))
            yield event.plain_result(line)
        except Exception as e:  # noqa: BLE001 - 戳一戳失败不能打断别的事
            logger.warning(f"[inbox] 戳一戳处理失败: {type(e).__name__} {e}")

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

    @staticmethod
    def _grams4(text: str) -> set:
        t = " ".join(str(text or "").split())
        if len(t) <= 4:
            return {t} if t else set()
        return {t[i:i + 4] for i in range(len(t) - 3)}

    @filter.on_llm_request()
    async def inject_intent(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            sender = str(event.get_sender_id() or "")
            if sender and sender == self.bot_qid:
                return
            # 记忆注入不能依赖 Mixin 的 @filter 是否被框架收集到；主流程显式调一次，
            # memory_inject 内部有 _memory_injected 幂等保护，重复调用不会拼两遍。
            try:
                await self.memory_inject(event, req)
            except Exception as e:  # noqa: BLE001 - 记忆失败不能打断对话
                logger.warning(f"[识海行为] 记忆注入调用失败: {type(e).__name__} {e}")
            # 隐私追问门：在 LLM 之前判定"这句话在问什么"，按性质给结论。
            # 必须放在这里 —— 下面有两处 return 会在非主人会话提前退出，
            # 放到后面就永远执行不到，而那恰恰是最需要它的场景。
            try:
                _pg = privacy_mod.guard(
                    str(getattr(event, "message_str", "") or ""),
                    viewer_is_owner=(sender == str(self.owner_qid or "")))
                if _pg:
                    req.prompt = (str(req.prompt or "") + "\n\n" + _pg)
            except Exception as e:  # noqa: BLE001 - 门失败不能打断对话
                logger.warning(f"[识海行为] 隐私门失败: {type(e).__name__} {e}")
            # E 组（自我状态分区）：非主人会话拿自己的 z_s + z_r，
            # 不含主人的 G_active / fire_pending / last_decision。
            if sender != self.owner_qid:
                try:
                    _grp = self._experiment_group()
                except Exception:  # noqa: BLE001
                    _grp = "d_full"
                if _grp == "e_partitioned":
                    _block = self._session_self_block(
                        sender, str(getattr(event, "unified_msg_origin", "") or ""))
                    if _block:
                        req.prompt = str(req.prompt or "") + "\n\n" + _block
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
            # 去重（省 Token）：G_active 的事件节点与 memory_inject 注入的旧事摘要
            # 常是同一件事写两遍；与已注入记忆 4 字片段重叠 ≥0.5 的条目剔除。
            mem_texts = list(getattr(req, "_memory_texts", []) or [])
            if mem_texts:
                kept: list = []
                for m in pkg["G_active"]:
                    g1 = self._grams4(str(m.get("content", "")))
                    if g1 and any(
                            len(g1 & self._grams4(t)) / max(1, len(g1)) >= 0.5
                            for t in mem_texts):
                        continue
                    kept.append(m)
                pkg["G_active"] = kept
            extra = self._decision_block(state)
            # 指代桥：主人说「她/他」时把当前焦点对象显式写进提示词。
            # 只在这一句真的出现代词时才加（约 20 token），避免每轮白付。
            focus_line = ""
            key = str(getattr(event, "unified_msg_origin", "") or "")
            msg = str(getattr(event, "message_str", "") or "")
            if key and sender == self.owner_qid:
                focus = self._update_focus(key, msg)
                pron = next((p for p in _FOCUS_PRONOUNS if p in msg), "")
                if focus and pron:
                    focus_line = (f"\n【当前焦点】「{pron}」指最近提到的那位：{focus}"
                                  "。这一句如果没给新的名字，就按这个对象理解，别反问「她是谁」。")
            # 意图包/决策档案挪到当前消息末尾而非 system_prompt：
            # system+历史保持恒定才能命中模型前缀缓存（同 dafeiyu_core 的做法）。
            # 语片模式：本地判这一轮用编排还是整段说完。
            # free 时必须换掉配置里那段分条指令——两段同时给会互相打架，
            # "最后一句轻收"这条更结构化的指令会把「我去给你倒杯热水」压成收尾。
            try:
                _msg = str(getattr(event, "message_str", "") or "")
                _ez = 0.0
                try:
                    _eb = self._emotion_baseline()
                    if _eb is not None:
                        _ez = float(_eb.peek(float(state.get("e_dom_value") or 0.0)))
                except Exception:  # noqa: BLE001 - 拿不到基线就按 0 处理
                    _ez = 0.0
                _mode = utterance_mod.mode_for(
                    e_z=_ez,
                    urgency=float(state.get("urge") or 0.0) / 3.0,
                    comforting=utterance_mod.looks_comforting(_msg))
                _utt_rule = utterance_mod.rule_for(_mode)
            except Exception:  # noqa: BLE001
                _utt_rule = self.intent_instruction_passive
            req.prompt = (str(req.prompt or "") + "\n\n"
                          + intent_mod.format(pkg)
                          + ("\n" + extra if extra else "")
                          + focus_line
                          + "\n" + _utt_rule)
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
        # 记忆 v2：衰减推进 + 情绪峰值冻结 + 异步固结（读链路在 memory_inject）
        try:
            await self.memory_watch(state)
        except Exception as e:  # noqa: BLE001 - 记忆故障不能拖垮行为层
            logger.warning(f"[识海行为] 记忆v2推进失败: {type(e).__name__} {e}")
        # 关系层 z_r(o)：与引擎同频 0.5h 步长积分（论文 6.10.4）
        try:
            self.memory_tick_relation()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[识海行为] 关系层推进失败: {type(e).__name__} {e}")
        # 生活任务层：确定性即时通道，每轮先跑；不与念头阈值抢触发
        try:
            await self.task_engine.tick(time.time(), state)
        except Exception as e:  # noqa: BLE001 - 任务层故障不能拖垮行为层
            logger.warning(f"[识海行为] 任务层推进失败: {type(e).__name__} {e}")
        # 承诺主动出口：到点的账自己冒出来（按时间被看见，不依赖相似度召回，
        # 也不与念头阈值抢触发——两者是并行通道）
        try:
            await self._remind_commitments(state)
        except Exception as e:  # noqa: BLE001 - 提醒故障不能拖垮行为层
            logger.warning(f"[识海行为] 承诺提醒推进失败: {type(e).__name__} {e}")
        # 课表 B 档：到课前提醒窗口主动发一次，fired 落盘去重
        try:
            await self._check_class_reminders()
        except Exception as e:  # noqa: BLE001 - 课表提醒故障不能拖垮心跳
            logger.warning(f"[课表] 课前提醒推进失败: {type(e).__name__} {e}")
        # 晨间简报：一天一条，把当天还没做完的账一次说清（没内容就不打扰）
        try:
            await self._maybe_morning_brief()
        except Exception as e:  # noqa: BLE001 - 简报故障不能拖垮行为层
            logger.warning(f"[晨间简报] 推进失败: {type(e).__name__} {e}")
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
        # 抢话判断：主人正在连着说，她插一句「我去给你倒杯热水」就从关心变成打断。
        # 只拦"开口"三类之外的文字行为；说说/画画/唱歌不是对话，不受此限。
        # 手动 behavior_fire_now 不走这里——那是主人明确要求"你现在找我一次"。
        if str(action.get("id", "") or "") not in ("post_feed", "draw_share", "sing_cover"):
            try:
                _hold_n, _hold_gap, _hold_last = self._recent_for_hold()
                if utterance_mod.should_hold(_hold_n, _hold_gap, _hold_last):
                    logger.info("[识海行为] 对方还在说（%d 条 / 间隔 %.1f 秒），本次推迟开口",
                                _hold_n, _hold_gap)
                    self._last_fire_emitted_at = emitted_at
                    # 不写 ok=False：这是时机不对，不是行为失败，不该扣它的 Q。
                    self._write_fired({"action": "", "text": "", "anchor_node": "",
                                       "emitted_at": emitted_at, "skip": True,
                                       "hold": True})
                    return
            except Exception as e:  # noqa: BLE001 - 判断失败不能挡住开口
                logger.debug(f"[识海行为] 抢话判断失败: {type(e).__name__} {e}")
        outcome = await self._execute(action, anchor, state, pending)
        if not outcome or not outcome.get("ok"):
            reason = str((outcome or {}).get("error", "") or "未知原因")
            logger.warning("[识海行为] 行为「%s」执行失败：%s",
                           action.get("label"), reason)
            if str(action.get("needs", "") or "") == "ai_cover":
                # B：音源/版权类失败是持续性故障 → 熔断一小时（见 COVER_FAIL_COOLDOWN）
                self._cover_fail_at = time.time()
            if (outcome or {}).get("retry"):
                return  # 生成端临时失败，不 ack，下一轮再试
            self.evolution.record(str(action.get("id", "")), False,
                                  kind=str(action.get("category", "")),
                                  text=reason)
            self._last_fire_emitted_at = emitted_at
            # C：skip 回执带上失败的动作与 ok=False，核心据此扣该行为的 Q。原来
            # action 留空，q_update 见空 action 直接 return——失败在 Q 表里不留
            # 痕迹，被历史成功抬高过的出口（如 sing_cover）下次还会被选中。
            self._write_fired({"action": str(action.get("id", "") or ""),
                               "text": "", "anchor_node": "",
                               "emitted_at": emitted_at, "skip": True,
                               "ok": False,
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

    async def _remind_commitments(self, state: Dict[str, Any],
                                  max_n: int = 3) -> None:
        """承诺主动出口：把即将到期/已到期的账，主动发给主人一次。

        为什么复用本循环而不另起守护线程 + heapq：这个插件已经有一条 asyncio
        定时循环（watcher_interval 秒/轮），跨线程发消息要走
        run_coroutine_threadsafe，崩溃面比现在大；而这里每轮只扫一遍内存里的
        dict（几十条，微秒级），不会空转烧 CPU。去重由 reminded_ts 承担，
        所以多轮也只提醒一次。
        """
        ledger = getattr(self, "_commit", None)
        if ledger is None:
            return
        try:
            due = ledger.pending_reminders(time.time())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[承诺] 取待提醒失败: {type(e).__name__} {e}")
            return
        if not due:
            return
        owner = str(getattr(self, "owner_qid", "") or "")
        for item in due[: max(1, int(max_n))]:
            try:
                ledger.guard_owner_target(self.sender.target, owner)
                msg = ledger.format_reminder(item)
                ok = await self.sender.send(msg)
                if not ok:
                    logger.warning("[承诺] 提醒发送失败，下轮重试：%s", item.text[:30])
                    continue
                ledger.mark_reminded(item.cid)
                try:  # 主动消息绕过 event 链路，不入史她会以为"我没说过"
                    await self._remember_self_line(
                        self.sender.target, msg, tag="承诺提醒")
                except Exception:  # noqa: BLE001
                    pass
                logger.info("[承诺] 已主动提醒：%s（due=%s）",
                            item.text[:30], item.due_text)
            except PermissionError as e:
                logger.error("[承诺] 拦截越界提醒：%s", e)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[承诺] 单条提醒异常: {type(e).__name__} {e}")

    # ---------- 课表：配置、免打扰、课前提醒 ----------

    def _schedule_cfg(self) -> Dict[str, Any]:
        cfg = self.config.get("schedule")
        return cfg if isinstance(cfg, dict) else {}

    @staticmethod
    def _now_is_quiet(now: float) -> bool:
        """与 tasks.py 的 quiet_hours [23,7) 同口径。"""
        h = time.localtime(now).tm_hour
        return h >= 23 or h < 7

    async def _check_class_reminders(self) -> None:
        cfg = self._schedule_cfg()
        if not cfg.get("enabled", True):
            return
        rem = cfg.get("reminder")
        rem = rem if isinstance(rem, dict) else {}
        if not rem.get("enabled", True):
            return
        lead = int(rem.get("lead_minutes", 20) or 20)
        quiet = (bool(rem.get("respect_quiet", True))
                 and self._now_is_quiet(time.time()))
        for day_key, key, msg in self._schedule.due_reminders(
                time.time(), lead, quiet):
            ok = await self.sender.send(msg)
            if not ok:
                logger.warning("[课表] 提醒发送失败，下轮重试：%s", msg[:20])
                continue
            self._schedule.mark_fired(day_key, key)
            try:
                await self._remember_self_line(
                    self.sender.target, msg, tag="课前提醒")
            except Exception:  # noqa: BLE001
                pass
            logger.info("[课表] 已发课前提醒：%s", msg[:20])

    # ---------- 晨间简报（一天一条，有内容才发） ----------

    def _brief_state_path(self) -> str:
        """简报去重标记的落点：plugin_data 下，不跟着插件目录走。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            base = os.path.join(str(get_astrbot_plugin_data_path()), "shihai_behavior")
        except Exception:  # noqa: BLE001 - 取不到就退到插件目录，别让简报整个失效
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_data")
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            pass
        return os.path.join(base, "brief_state.json")

    def _brief_sent(self, day: str) -> bool:
        try:
            with open(self._brief_state_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return str((data or {}).get("day", "")) == day
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def _brief_mark(self, day: str) -> None:
        try:
            with open(self._brief_state_path(), "w", encoding="utf-8") as fh:
                json.dump({"day": day}, fh, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[晨间简报] 标记落盘失败: {e}")

    async def _maybe_morning_brief(self, force: bool = False,
                                   send: bool = True) -> str:
        """晨间简报：到点一次，把当天还没做完的承诺列清楚。

        两条取舍：
        1) 只讲承诺账——这是唯一已经有真实数据、且"到点真的有用"的来源。
           没有可说的就整条不发，不做"早安 + 天气"式的每天一条噪音。
        2) 去重键是日期字符串，内存 + 落盘双份：内存挡同一早晨的多轮，
           落盘挡"重启之后又发一遍"。
        """
        from datetime import datetime

        cfg: Dict[str, Any] = {}
        try:
            raw = self.config.get("morning_brief")
            cfg = raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001
            cfg = {}
        if not force and not bool(cfg.get("enabled", True)):
            return ""
        hour, minute = 7, 30
        try:
            hour = int(cfg.get("hour", 7) or 7)
            minute = int(cfg.get("minute", 30) or 30)
        except (TypeError, ValueError):
            pass

        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        if not force:
            if now.hour * 60 + now.minute < hour * 60 + minute:
                return ""
            if now.hour >= 12:  # 中午之后不再补发，免得下午突然冒出"早安"
                return ""
            if self._brief_day == day or self._brief_sent(day):
                self._brief_day = day
                return ""

        ledger = getattr(self, "_commit", None)
        rows = []
        if ledger is not None:
            try:
                rows = ledger.open_items("owner", time.time())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[晨间简报] 取承诺失败: {type(e).__name__} {e}")

        # C 档：当天课表块（开关关闭或无数据时为空串）
        sched_block = ""
        if self._schedule_cfg().get("morning_brief", True):
            try:
                w = self._schedule.current_week()
                sched_block = self._schedule.format_day(
                    w, now.weekday() + 1, title="今天的课：")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[晨间简报] 取课表失败: {type(e).__name__} {e}")

        rows = sorted(rows, key=lambda x: (x.due_ts or float("inf")))
        items: list[str] = []
        for x in rows[:5]:
            text = str(getattr(x, "text", "") or "").strip()
            if not text:
                continue
            due_ts = float(getattr(x, "due_ts", 0) or 0)
            if due_ts:
                when = (str(getattr(x, "due_text", "") or "").strip()
                        or time.strftime("%m-%d %H:%M", time.localtime(due_ts)))
                items.append(f"- {text}（{when}）")
            else:
                items.append(f"- {text}")

        if not items and not sched_block:
            if not force:
                self._brief_day = day  # 今天没账也没课，不再重试
            return ""

        weekday = "一二三四五六日"[now.weekday()]
        head = f"早。今天 {now.month} 月 {now.day} 号，周{weekday}。"
        parts = [head]
        if sched_block:
            parts.append(sched_block)
        if items:
            parts.append("你答应过、还没做完的：\n" + "\n".join(items)
                         + "\n我不催，到点我自己记着说一声。")
        msg = "\n".join(parts)

        if not send:
            return msg
        try:
            if items:  # 只有含承诺账时才校验目标，纯课表不拦
                ledger.guard_owner_target(self.sender.target, self.owner_qid)
            ok = await self.sender.send(msg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[晨间简报] 发送异常: {type(e).__name__} {e}")
            return ""
        if not ok:
            logger.warning("[晨间简报] 发送失败，下一轮重试")
            return ""
        self._brief_day = day
        self._brief_mark(day)
        try:  # 主动消息绕过 event 链路，不入史她会以为"我没说过"
            await self._remember_self_line(self.sender.target, msg, tag="晨间简报")
        except Exception:  # noqa: BLE001
            pass
        logger.info("[晨间简报] 已发送 %d 条（%s）", len(items), day)
        return msg

    @filter.command("晨报", alias={"晨间简报", "早报"})
    async def cmd_morning_brief(self, event: AstrMessageEvent):
        """手动看一眼今天的晨间简报。用法：/晨报"""
        sid = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if str(self.owner_qid) and sid != str(self.owner_qid):
            yield event.plain_result("这个只给鑫鑫看。")
            return
        text = await self._maybe_morning_brief(force=True, send=False)
        yield event.plain_result(text or "今天没有要催你的账。")

    @filter.command("课表", alias={"课程", "今天的课"})
    async def cmd_schedule(self, event: AstrMessageEvent):
        """手动查今天的课。用法：/课表"""
        sid = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if str(self.owner_qid) and sid != str(self.owner_qid):
            yield event.plain_result("这个只给鑫鑫看。")
            return
        if not self._schedule_cfg().get("enabled", True):
            yield event.plain_result("课表功能现在关着。")
            return
        now = datetime.now()
        week = self._schedule.current_week(now.timestamp())
        block = self._schedule.format_day(
            week, now.weekday() + 1, title="今天的课：")
        yield event.plain_result(f"第{week}周。\n{block}")

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
            if str(action.get("id", "")) == "promise_followup":
                # 08:23 那次事故：约定是「到时候说声早安」，她却发成
                # 「现在算到时候了没？」——把已经说好的事又推回去让对方确认，
                # 听起来就是个空洞的招呼。约定到点了就直接做，别反问时机。
                system += (
                    "\n这条是答应过对方、到点该回访的事：如果当初约定的是一件"
                    "具体动作（说早安、道晚安、提醒某件事、交付某样东西），"
                    "现在就直接把它做出来，不要反问「到时候了没」「现在算不算」"
                    "「你还记得吗」——那等于把已经说好的事又推回给对方确认。"
                    "只有约定内容本身确实含糊、无法执行时，才允许问一句具体是哪件事。")
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

    def _recent_for_hold(self, window: float = 90.0):
        """主动开口前的"对方是不是还在说"探测 → (条数, 距上一条秒, 最后一条文本)。

        数据只能来自 transcript：对话层有 sender_id / origin，事实层没有。
        所以这件事本身就是"只能用对话层做"的判断。

        origin 用尾部匹配而不是整串相等 —— 历史上 platform_id 换过，库里同时
        存在 `napcat:FriendMessage:<qq>` 和 `qq:FriendMessage:<qq>` 两种写法。
        """
        try:
            now = time.time()
            rows = self._transcript.get_range(now - window, now)
        except Exception as e:  # noqa: BLE001 - 探测失败按"没人说话"处理
            logger.debug(f"[识海行为] 抢话探测失败: {type(e).__name__} {e}")
            return 0, 999.0, ""
        target = str(getattr(self.sender, "target", "") or "")
        tail = target.split(":", 1)[-1] if ":" in target else target
        rows = [r for r in rows
                if str(r.get("role", "")) == "user"
                and (not tail or str(r.get("origin", "")).endswith(tail))]
        if not rows:
            return 0, 999.0, ""
        last = rows[-1]
        try:
            gap = max(0.0, now - float(last.get("ts") or 0))
        except (TypeError, ValueError):
            gap = 999.0
        return len(rows), gap, str(last.get("content", "") or "")

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
        # 主动开口也要入史：框架只在"正常回复"路径写回 history，主动消息绕过
        # event.send（走 context.send_message），所以这句话在她的上下文里原本
        # 不存在。下一轮她会以为"我没说过"，甚至把自己发的内容脑补成别的话。
        await self._remember_self_line(self.sender.target, text, tag="主动开口")
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
        await self._remember_self_line(
            self.sender.target, f"画好了，给你。（画面：{prompt}）", tag="主动发图")
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
        await self._remember_self_line(self.sender.target, text, tag="唱歌给你听")
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

    @filter.llm_tool("query_schedule")
    async def query_schedule(self, event: AstrMessageEvent, day: str = "today"):
        """查某天的课表。主人问「今天/明天有什么课」「几点上课」「这周第几周」时用。

        Args:
            day (str): "today" 或 "tomorrow"
        """
        is_tomorrow = str(day).strip().lower() in ("tomorrow", "明天", "1")
        now = datetime.now() + (timedelta(days=1) if is_tomorrow
                                else timedelta())
        week = self._schedule.current_week(now.timestamp())
        title = ("明天" if is_tomorrow else "今天") + "的课："
        block = self._schedule.format_day(week, now.weekday() + 1, title=title)
        return f"第{week}周。\n{block}"

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

    @filter.llm_tool("recall_transcript")
    async def recall_transcript(self, event: AstrMessageEvent, keyword: str,
                               days: int = 30, role: str = ""):
        """检索你和对方过去对话的逐字原文。需要确认某句话当初到底怎么说、回忆某次具体对话时用；不是每轮都调，只在需要原话时用，返回内容不会常驻上下文。

        Args:
            keyword (str): 要找的关键词，如人名、地点、事情
            days (int): 往前找多少天，默认 30
            role (str): 可选，user 或 assistant，只看某一方
        """
        kw = str(keyword or "").strip()
        if not kw:
            return "需要给一个关键词才能检索原话。"
        since = None
        try:
            since = time.time() - max(1, int(days)) * 86400
        except (TypeError, ValueError):
            pass
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        # origin 为空时不检索：否则 search 会退化成全库扫描，
        # 把别的会话（含主人私聊）的原话捞到当前这个人的上下文里。
        if not origin:
            return "这个会话没有可检索的记录。"
        is_owner = str(event.get_sender_id() or "") == str(self.owner_qid or "")
        r = (role or "").strip()
        if r not in ("user", "assistant"):
            r = None
        rows = await asyncio.to_thread(
            self._transcript.search, kw,
            origin=origin, since=since, role=r,
            limit=5, snippet=200)
        if not rows:
            return f"没找到和「{kw}」有关的原话。"
        who_user = "鑫鑫" if is_owner else "对方"
        lines = ["【逐字原话检索】关键词：" + kw]
        for it in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(it.get("ts", 0)))
            who = who_user if it.get("role") == "user" else "小满"
            body = str(it.get("content", "") or "")
            # 非本人视角：原话里可能带着主人的专名、号码、具体日期。
            # 按 D2 改写（去专名保轮廓），既守得住又能回答"当时大概说了什么"。
            if not is_owner:
                try:
                    body = self._mem_disclose(body, 2) or "（这段涉及别人的私事，跳过）"
                except Exception:  # noqa: BLE001
                    body = "（这段涉及别人的私事，跳过）"
            tag = (f"（{it.get('kind')}）"
                   if it.get("kind") not in (None, "text") else "")
            lines.append(f"{when} {who}{tag}：{body}")
        lines.append("（这些是当时的逐字原文，可直接引用；没列出的不要补编。）")
        return "\n".join(lines)

    @filter.llm_tool("remember_commitment")
    async def remember_commitment(self, event: AstrMessageEvent, text: str,
                                  when: str = ""):
        """把「他让你到点提醒他的事」记进承诺账簿，到点你自己主动提醒他。他说「帮我记着」「别忘了」「到时候提醒我」并带具体时间时用；「改天」「回头」这类含糊时间不要记。

        Args:
            text (str): 这件事本身，用他的原话，一句话；不要把时间再抄一遍
            when (str): 时间原话，如「明天8点」「周三下午三点」「11月15日9点」
        """
        body = str(text or "").strip()
        if not body:
            return "要记的事还是空的，你先把事情说出来我再记。"
        try:
            tag = self._mem_owner_tag(
                str(event.get_sender_id() or ""),
                str(getattr(event, "unified_msg_origin", "") or ""))
        except Exception:  # noqa: BLE001
            tag = "owner"
        ledger = getattr(self, "_commit", None)
        if ledger is None:
            return "账簿这会儿没打开，晚点再让我记。"
        ok, msg = ledger.remember(
            tag, body, str(when or "").strip(),
            src=str(getattr(event, "unified_msg_origin", "") or ""))
        if ok:
            logger.info("[commit] 工具立账 tag=%s when=%s 内容=%.40s",
                        tag, str(when or "").strip(), body)
        else:
            logger.info("[commit] 工具立账被拒：%s", msg)
        return msg
