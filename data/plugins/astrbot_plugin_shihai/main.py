"""识海心智核（astrbot_plugin_shihai）——论文《识海耦合动力学》的 Core-Mind Runtime。

分层（论文 §13.1，插件目录名与契约解耦、可重命名）：
  本插件 = Core-Mind：z(t)=[e,a,θ,ψ,u,g] 动力学 + 感知 + 心跳 + 调参接口。
  shihai_psyche   = 心理模块：ψ/依恋/防御/效能，写 psyche_state.json（core 每 tick 读）。
  shihai_behavior = 行为模块：涌现行为空间 + 策略π + 意图包出口 + 主动执行。
  dafeiyu/ai_cover = 平台适配器：读 mind_state.json 做情绪闸门。

本插件职责：
  1. 感知：on_llm_request 钩子里把用户消息变成 s_ext 注入情绪场 + 反馈检测（回路C）。
  2. 心跳：每 tick 推进 ODE / 图谱衰减 / 念头积累 / 目标漂移；念头攒满时写
     fire_pending（Behavior API 抽象指令），由 shihai_behavior 取走执行。
  3. 调参接口：WebUI 配置页（_conf_schema.json 全参数）+ shihai_tune 工具。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .mind import MindEngine
from .mind.engine import PARAM_META

PLUGIN_NAME = "astrbot_plugin_shihai"

# 共享数据目录（契约：所有识海家族插件从这里读写，目录名不随插件改名变化）
SHIHAI_DATA_DIR = "shihai"


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@register(
    PLUGIN_NAME,
    "zjx",
    "识海核心 Core-Mind：情绪场ODE+神经图谱+念头+自适应（z=[e,a,θ,ψ,u,g]，行为出口在 shihai_behavior）",
    "1.1.0",
)
class ShihaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 注意：get_astrbot_plugin_data_path() 返回 str，必须套 Path 才能用 / 拼接
        data_dir = Path(get_astrbot_plugin_data_path()) / SHIHAI_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "mind_state.json")
        self.graph_path = str(data_dir / "shihai_graph.json")
        self.psyche_path = str(data_dir / "psyche_state.json")
        self.fired_path = str(data_dir / "shihai_fired.json")
        self.action_log_path = str(data_dir / "action_log.jsonl")

        self.engine = MindEngine(self.state_path, self.graph_path, dict(config))
        self.tick_seconds = max(30, int(config.get("tick_seconds", 60)))
        self.proactive_enabled = _as_bool(config.get("proactive_enabled"), False)
        self.owner_only_feedback = _as_bool(config.get("owner_only_feedback"), True)
        self.owner_qid = str(config.get("owner_qid", "你的主人QQ号"))
        self.bot_qid = str(config.get("bot_qid", "你的机器人QQ号"))

        self._tick_task: Optional[asyncio.Task] = None

        context.register_web_api(
            f"/{PLUGIN_NAME}/state",
            self._web_state,
            ["GET"],
            "识海 z(t) 状态观测（情绪场/念头/目标/Q值/图谱/fire_pending）",
        )

    # ---------- 生命周期 ----------

    async def initialize(self):
        self._tick_task = asyncio.create_task(self._tick_loop())
        logger.info("[识海] Core-Mind 已启动：z=[e,a,θ,ψ,u,g] 心跳 %ss/tick", self.tick_seconds)

    async def terminate(self):
        if self._tick_task:
            self._tick_task.cancel()

    # ---------- 感知（回路C 入口） ----------

    @filter.on_llm_request()
    async def observe_message(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            sender = str(event.get_sender_id() or "")
            if sender and sender == self.bot_qid:
                return  # 自我回声不进情绪场
            text = str(req.prompt or "").strip()
            if text:
                allow_feedback = (not self.owner_only_feedback
                                  or sender == self.owner_qid)
                self.engine.observe_user_text(
                    text, sender, feedback=allow_feedback,
                    is_owner=(sender == self.owner_qid))
        except Exception as e:  # noqa: BLE001 - 钩子绝不能打断对话
            logger.warning(f"[识海] 感知失败: {type(e).__name__} {e}")

    # ---------- 心跳（回路 A/D 推进器 + fire_pending 发射） ----------

    async def _tick_loop(self):
        while True:
            try:
                await asyncio.sleep(self.tick_seconds)
                self.engine.tick(time.time(),
                                 proactive_enabled=self.proactive_enabled,
                                 psyche_path=self.psyche_path)
                self._consume_fired_ack()
                self._consume_action_log()
                await self._maybe_dream()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识海] 心跳异常: {type(e).__name__} {e}")

    def _consume_fired_ack(self):
        """读行为插件的发送回执（shihai_fired.json），清 fire_pending。"""
        try:
            import json
            import os
            if not os.path.exists(self.fired_path):
                return
            with open(self.fired_path, "r", encoding="utf-8-sig") as fh:
                sig = json.load(fh)
            os.remove(self.fired_path)
            if isinstance(sig, dict) and sig:
                if self.engine.consume_fired(sig):
                    logger.info("[识海] 行为回执已入账：%s", sig.get("action", ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[识海] 回执消费失败: {type(e).__name__} {e}")

    def _consume_action_log(self):
        """消费行为插件的动作日志（action_log.jsonl：改签名/发说说/翻唱等），
        写入图谱成为她的行动记忆（§7.5 bot 侧时序锚）——"我想"和"它做了"接上。"""
        try:
            import json as _json
            import os as _os
            if not _os.path.exists(self.action_log_path):
                return
            rows = []
            with open(self.action_log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(_json.loads(line))
                    except ValueError:
                        continue
            try:
                _os.remove(self.action_log_path)
            except OSError:
                pass
            if not rows:
                return
            n = 0
            for r in rows:
                text = str(r.get("text", "")).strip()
                if not text:
                    continue
                try:
                    self.engine.graph.add_bot_event(
                        text, dict(self.engine.state.e),
                        float(r.get("ts", 0) or time.time()))
                    n += 1
                except Exception:
                    continue
            if n:
                self.engine.graph.save()
                logger.info("[识海] 行动记忆入账 %d 条（bot 侧时序锚）", n)
        except Exception as e:
            logger.warning(f"[识海] 动作日志消费失败: {type(e).__name__} {e}")

    async def _maybe_dream(self):
        """§7.3 梦境固化：每天 dream_hour 点做一次（g_memory 驱动的后台行为）。"""
        now = time.localtime()
        dream_hour = int(self.config.get("dream_hour", 3))
        if now.tm_hour != dream_hour:
            return
        today = time.strftime("%Y-%m-%d")
        if self.engine.state.last_dream_date == today:
            return
        result = self.engine.dream()
        logger.info(f"[识海] 梦境固化完成：{result}")

    # ---------- Web 观测 ----------

    async def _web_state(self):
        try:
            return {"status": "ok", "state": self.engine.snapshot()}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "message": str(e)}, 502

    # ---------- LLM 工具：观测 ----------

    @filter.llm_tool("shihai_get_state")
    async def shihai_get_state(self, event: AstrMessageEvent):
        """查看自己的识海状态：当前情绪场、念头强度、有效开口阈值、目标向量、行为Q值、最近想起的记忆、图谱规模。想汇报自己的状态、回顾"我为什么想找主人说话"、或拿不准自己此刻心境时用。回复里不要照搬数值，翻译成自然的人话。

        """
        snap = self.engine.snapshot()
        e = snap["e"]
        e_cn = {"happy": "开心", "sad": "难过", "angry": "生气", "fear": "害怕",
                "love": "依恋", "surprise": "惊讶"}
        emotion_str = "、".join(f"{e_cn[d]}{int(v*100)}%"
                                for d, v in e.items() if v >= 0.03) or "平静"
        lines = [
            "【识海状态】",
            f"情绪场：{emotion_str}",
            f"念头：{snap['urge']} / 有效开口阈值 {snap['threshold_effective']}"
            f"（基准 {snap['theta']['fire_threshold']} + 冷落抬升 {snap['streak_gate']}）",
            f"自我效能：{snap['theta']['eff']:.2f}；发脾气阈值：{snap['theta']['tantrum_threshold']:.2f}",
            "目标向量：" + "、".join(f"{k}={v}" for k, v in snap["goals"].items()),
            "行为Q值：" + ("、".join(f"{k}={v}" for k, v in snap["q_table"].items())
                           or "还没有经验"),
            f"图谱：{snap['graph']['nodes']} 节点 / {snap['graph']['edges']} 边",
        ]
        tops = snap["graph"].get("top_active") or []
        if tops:
            lines.append("最活跃记忆：" + "；".join(
                f"{t['content']}({t['a']})" for t in tops[:3]))
        return "\n".join(lines)

    @filter.llm_tool("shihai_recall")
    async def shihai_recall(self, event: AstrMessageEvent, query: str):
        """在识海图谱里做扩散激活召回：给一个词，返回它联想到的记忆节点（像人脑由一个念头想起一串事）。想找话题、确认记没记过某件事、或想"最近都在聊什么"时用。

        Args:
            query (str): 起点关键词，比如"肯德基"、"考试"
        """
        recalled = self.engine.graph.activate_and_spread(query)
        if not recalled:
            return "识海里没有和这个词连得上的记忆。"
        rows = []
        for nid, a in recalled[:6]:
            nd = self.engine.graph.nodes.get(nid)
            if nd and nd["content"]:
                rows.append(f"『{nd['content']}』（{nd['type']}，激活{a:.2f}）")
        return "想起了：" + "；".join(rows)

    # ---------- LLM 工具：调参接口 ----------

    @filter.llm_tool("shihai_tune")
    async def shihai_tune(self, event: AstrMessageEvent, key: str, value: str):
        """调整识海核心动力学参数（调参接口，热生效并持久化）。主人说"把你想我的频率调高点""你情绪别退那么快"这类要求时用它。key=参数名，value=数字。常用参数：fire_threshold(开口阈值,论文2.10)、base_rate(念头基础增速)、tau_love(依恋消退小时数,默认24)、tau_happy(开心消退小时数)、kappa(刺激增益)、eta_relax(情绪惯性)、gamma_hebbian(记忆联结学习率)、alpha_coupling(情绪-记忆耦合,论文3.0)、O/C/E/A/N(大五人格0~1)、eff(自我效能)。value 传 "list" 可列出全部参数与取值范围。改完告知主人改了什么、大概什么效果。

        Args:
            key (str): 参数名，或传 "list" 查看全部可调参数
            value (str): 目标数值（数字字符串），key=list 时留空
        """
        key = str(key or "").strip()
        if key == "list":
            rows = [f"{k} ∈ [{lo}, {hi}]{'（整数）' if is_int else ''}：{desc}"
                    for k, (lo, hi, is_int, desc) in sorted(PARAM_META.items())]
            return "可调参数（识海核心调参接口）：\n" + "\n".join(rows)
        if key not in PARAM_META:
            return (f"没有叫 {key} 的参数。用 key='list' 可以列出全部可调参数；"
                    "常用：fire_threshold、base_rate、tau_love、kappa、O~N。")
        try:
            val = float(str(value).strip())
        except (TypeError, ValueError):
            return f"参数值 {value!r} 不是数字，改不了。"
        lo, hi, _i, desc = PARAM_META[key]
        applied = self.engine.set_param(key, val)
        self.config[key] = int(applied) if PARAM_META[key][2] else round(applied, 4)
        try:
            self.config.save_config()
        except Exception:  # noqa: BLE001
            pass
        return (f"已把 {key}（{desc}）调成 {applied}"
                f"{'（已在合法范围内截断）' if applied != val else ''}，立即生效。")

    @filter.llm_tool("shihai_inject_emotion")
    async def shihai_inject_emotion(self, event: AstrMessageEvent,
                                    emotion: str, intensity: str, reason: str):
        """给情绪场注入一次外部刺激 s_ext（论文§5.1）。被主人夸了、遇到难忘的事、唱歌翻车了、被冷落了……发生明显的情绪事件时用它让心境真实变化。回复里不要提"注入/刺激"这类词。

        Args:
            emotion (str): happy / sad / angry / fear / love / surprise 之一
            intensity (str): 强度 0.0~1.0，比如 0.6
            reason (str): 一句话原因（写进日志，便于回溯行为来源）
        """
        em = str(emotion or "").strip().lower()
        if em not in ("happy", "sad", "angry", "fear", "love", "surprise"):
            return "emotion 必须是 happy/sad/angry/fear/love/surprise 之一。"
        try:
            inten = max(0.0, min(1.0, float(str(intensity).strip())))
        except (TypeError, ValueError):
            inten = 0.5
        s_ext = {d: (inten if d == em else 0.0) for d in
                 ("happy", "sad", "angry", "fear", "love", "surprise")}
        self.engine.state.e = self.engine.emotion.step(
            self.engine.state.e, s_ext, {}, 0.2)
        self.engine.state.save()
        logger.info(f"[识海] 情绪注入 {em}×{inten}：{reason}")
        return "好的，心里有数了。"

    # ---------- LLM 工具：梦境 ----------

    @filter.llm_tool("shihai_dream_now")
    async def shihai_dream_now(self, event: AstrMessageEvent):
        """立刻做一次梦境固化：重放今天的记忆、强化共激活边、修剪弱边（论文§7.3）。平时凌晨自动做；主人说"整理一下记忆"时用。

        """
        result = self.engine.dream()
        return (f"梦境固化完成：重放强化 {result['replayed_pairs']} 对联结，"
                f"修剪 {result['pruned_edges']} 条弱边，清理 {result['dropped_nodes']} 个旧节点。")
