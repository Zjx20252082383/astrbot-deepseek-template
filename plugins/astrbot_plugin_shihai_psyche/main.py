"""识海心理模块（astrbot_plugin_shihai_psyche）——论文 §8 人格心理学建模。

分层契约：本插件拥有 psyche_state.json（ψ/效能/依恋/防御/条件反射/反馈计数），
核心 astrbot_plugin_shihai 每 tick 读取并应用到 z(t)——单向流动，无锁无竞争。
数据目录 data/plugin_data/shihai/（契约目录名，插件可重命名）。

调参接口：
  - WebUI 配置页：ψ 初值、依恋 att、防御风格、自我效能、反馈词典扩展
  - LLM 工具：psyche_tune（改人格/效能/防御）、psyche_report（心理侧写）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .psyche import profile

PLUGIN_NAME = "astrbot_plugin_shihai_psyche"

# 数值可调参数：key → (min, max, 说明)
PSYCHE_PARAMS = {
    "O": (0.0, 1.0, "开放性：认知失调时决定更新还是合理化"),
    "C": (0.0, 1.0, "尽责性：答应的事上不上心"),
    "E": (0.0, 1.0, "外向性：主动开口概率 ∝ E"),
    "A": (0.0, 1.0, "宜人性：发脾气阈值 ×(1+A)"),
    "N": (0.0, 1.0, "神经质：恐惧弛豫 τ×(1+N)，失败防御风格"),
    "eff": (0.0, 1.0, "自我效能：成功+0.05/失败-0.1 的基准"),
    "att": (0.0, 3.0, "依恋 att：用户回复一致性 → 主动开口频率"),
    "tantrum": (0.1, 2.0, "发脾气阈值基准（×(1+A) 后生效）"),
}

# psyche.theta 键 → ψ/θ 映射
_THETA_KEY = {"eff": "eff", "att": "send_threshold", "tantrum": "tantrum_threshold"}


@register(
    PLUGIN_NAME,
    "zjx",
    "识海心理模块：大五人格/依恋/认知失调/条件反射/防御机制/自我效能（论文§8，含调参接口）",
    "1.0.0",
)
class PsychePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(get_astrbot_plugin_data_path()) / "shihai"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = str(data_dir / "psyche_state.json")
        self.state = profile.load_state(self.state_path, dict(config))
        self._flush()

    def _flush(self):
        profile.save_state(self.state_path, self.state)

    # ---------- LLM 工具：调参接口 ----------

    @filter.llm_tool("psyche_tune")
    async def psyche_tune(self, event: AstrMessageEvent, key: str, value: str):
        """调整自己的心理参数（识海调参接口·心理面，热生效并持久化）。用户说"你脸皮厚一点""你胆子小点""别那么容易发火"这类要求时用。可调：O(开放性)、C(尽责性)、E(外向性)、A(宜人性)、N(神经质)、eff(自我效能)、att(依恋，越高越敢主动找用户)、tantrum(发脾气阈值)。取值都是 0~1（att 是 0~3）。value 传 "list" 列出全部。改完用人话告诉用户调了什么、大概什么效果。

        Args:
            key (str): 参数名，或传 "list" 查看全部
            value (str): 目标数值（数字字符串），key=list 时留空
        """
        key = str(key or "").strip()
        if key == "list":
            return "心理可调参数：\n" + "\n".join(
                f"{k} ∈ [{lo}, {hi}]：{desc}" for k, (lo, hi, desc) in PSYCHE_PARAMS.items())
        if key not in PSYCHE_PARAMS:
            return (f"没有叫 {key} 的心理参数。可调："
                    + "、".join(PSYCHE_PARAMS) + "（或 value=list 查看说明）。")
        try:
            val = float(str(value).strip())
        except (TypeError, ValueError):
            return f"参数值 {value!r} 不是数字，改不了。"
        lo, hi, desc = PSYCHE_PARAMS[key]
        val = max(lo, min(hi, val))
        if key in _THETA_KEY:
            self.state["theta"][_THETA_KEY[key]] = val
        else:
            self.state["psi"][key] = val
        self._flush()
        logger.info(f"[识海心理] {key} → {val}")
        return f"已把心理参数 {key}（{desc}）调成 {val:.2f}，立即生效。"

    # ---------- LLM 工具：心理侧写 ----------

    @filter.llm_tool("psyche_report")
    async def psyche_report(self, event: AstrMessageEvent):
        """生成自己的心理侧写：人格五维解读、依恋类型、自我效能、防御机制风格、历史反馈统计。用户想深入了解"你是个什么样的人/你的性格/你为什么这样"，或自己拿不准该用什么姿态说话时用。回复时把侧写融进人话里，禁止照搬字段名和数值。

        """
        return profile.profile_text(self.state)

    # ---------- LLM 工具：防御机制（论文 §8 表） ----------

    @filter.llm_tool("psyche_defense")
    async def psyche_defense(self, event: AstrMessageEvent, outcome: str, detail: str):
        """失败/受挫时按自己的防御机制风格消化它（论文§8：N/A 决定合理化/压抑/投射）。工具调用失败、唱歌翻车、被用户拒绝之后用；返回按当前人格该有的心理处理方式，照着这个姿态继续说话就好。回复中禁止提到"防御机制"四个字。

        Args:
            outcome (str): 发生了什么，比如"唱歌下载音源失败"
            detail (str): 一句话细节
        """
        style = profile.defense_style_desc(
            self.state.get("defense_style", "auto"), self.state["psi"])
        eff = self.state["theta"].get("eff", 0.6)
        eff_drop = 0.1 if eff > 0.3 else 0.05
        self.state["theta"]["eff"] = max(0.0, eff - eff_drop)
        self._flush()
        return (f"（{style}；自我效能 {eff:.2f}→{self.state['theta']['eff']:.2f}）"
                f"事件：{outcome}——{detail}。按这个姿态自然带过或自嘲，别过度道歉。")
