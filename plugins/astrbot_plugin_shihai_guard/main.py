"""识海·一致性守护（astrbot_plugin_shihai_guard）——框架适配层。

职责（只读 z(t)，不写快状态）：
  1. 生成前：把 R_t、when 标签、时序纪律、ψ 渲染的表达参数注入提示词；
  2. 生成前：跑四条不变量的审计，违规只降级不中断；
  3. 生成后：强度/时态校验，必要时降级输出。

配置项（data/config/astrbot_plugin_shihai_guard_config.json）：
  enabled           总开关
  r_t_limit         R_t 条数（默认 4）
  r_t_hours         R_t 时间窗（默认 24）
  inject_style      是否注入 ψ 渲染的表达参数
  postcheck         是否启用生成后校验
  intensity_tol     强度偏差容忍（默认 0.35）
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .guard import channels, persona, postcheck, timeline
from .guard.invariants import GuardAuditor

PLUGIN_NAME = "astrbot_plugin_shihai_guard"
SHIHAI_DATA_DIR = "shihai"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _flag(v: Any, default: bool = True) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _nodes_of(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for nid, nd in (graph.get("nodes", {}) or {}).items():
        if not isinstance(nd, dict):
            continue
        row = dict(nd)
        row["id"] = str(nid)
        out.append(row)
    return out


@register(
    PLUGIN_NAME, "zjx",
    "识海一致性守护：时序锚定 R_t + quote 隔离 + 表达归界 + 生成后校验（I1–I4）",
    "1.0.0",
)
class ShihaiGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config or {}
        data_dir = Path(get_astrbot_plugin_data_path()) / SHIHAI_DATA_DIR
        self.data_dir = data_dir
        self.graph_path = data_dir / "shihai_graph.json"
        self.state_path = data_dir / "mind_state.json"
        self.psyche_path = data_dir / "psyche_state.json"
        self.auditor = GuardAuditor(str(data_dir / "guard_trace.jsonl"))

    # -------------------------------------------------- 读取共享状态（只读）
    def _r_t(self) -> List[Dict[str, Any]]:
        nodes = _nodes_of(_read_json(self.graph_path))
        return timeline.build_r_t(
            nodes, now=time.time(),
            hours=float(self.config.get("r_t_hours", 24) or 24),
            limit=int(self.config.get("r_t_limit", 4) or 4),
        )

    def _g_active(self) -> List[Dict[str, Any]]:
        rows = sorted(_nodes_of(_read_json(self.graph_path)),
                      key=lambda r: -float(r.get("a", 0.0)))
        return channels.filter_facts(rows, limit=5)

    def _psi(self) -> Dict[str, float]:
        psy = _read_json(self.psyche_path)
        psi = psy.get("psi") or psy.get("bigfive") or {}
        return {k: float(v) for k, v in psi.items()
                if isinstance(v, (int, float))}

    # -------------------------------------------------- 生成前
    @filter.on_llm_request()
    async def inject_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """融合版注入职责（避免与 behavior 意图包双重注入）：

        - R_t / G_active / 时序纪律：由 shihai_behavior 的意图包出口负责
          （版本更强：含指代时间词换算与主动消息归属声明），此处不再重复注入；
        - 本插件只注入 behavior 没有的增量：ψ 渲染的表达参数（I3）；
        - 同时对 behavior 已组装好的完整提示词跑 I1–I4 审计并写 guard_trace。
        """
        if not _flag(self.config.get("enabled")):
            return
        try:
            r_t = self._r_t()
            g_active = self._g_active()
            psi = self._psi()

            blocks: List[str] = []
            if _flag(self.config.get("inject_style")):
                blocks.append(persona.render_style_block(psi))

            audit = self.auditor.audit(
                g_active=g_active,
                anchors=[],       # 锚点候选由主插件持有，此处留空即不审计
                r_t=r_t,
                persona_text=str(getattr(req, "system_prompt", "") or ""),
                m_packet={"r_t": r_t, "g_active": g_active, "psi": psi},
            )
            if not audit["i1"]["pass"]:
                logger.warning("[shihai-guard] R_t 时序非单调（审计告警，已记录）")
            if not audit["i3"]["pass"]:
                logger.warning(
                    f"[shihai-guard] I3 表达归界违规：persona 断言"
                    f"{audit['i3']['persona_violations']} 处")

            if blocks:
                req.system_prompt = (
                    str(getattr(req, "system_prompt", "") or "")
                    + "\n\n" + "\n\n".join(blocks)
                )
        except Exception as exc:  # noqa: BLE001 守护层自身不得中断对话
            logger.warning(f"[shihai-guard] inject failed: {type(exc).__name__} {exc}")

    # -------------------------------------------------- 生成后
    def _text_of(self, result: Any) -> str:
        txt = getattr(result, "text", "") or ""
        if txt:
            return str(txt)
        chain = getattr(result, "chain", None) or []
        parts: List[str] = []
        for comp in chain:
            parts.append(str(getattr(comp, "text", "") or ""))
        return "".join(parts)

    def _set_text(self, result: Any, new_text: str) -> bool:
        try:
            result.text = new_text
            return True
        except Exception:  # noqa: BLE001
            return False

    @filter.on_decorating_result()
    async def verify_output(self, event: AstrMessageEvent):
        if not _flag(self.config.get("postcheck")):
            return
        try:
            result = event.get_result()
            text = self._text_of(result)
            if not text:
                return
            state = _read_json(self.state_path)
            e = state.get("e") or {}
            if e:
                e_dom = max(e.items(), key=lambda kv: float(kv[1]))[0]
                e_val = float(e[e_dom])
            else:
                e_dom, e_val = "happy", 0.0
            chk = postcheck.check_against_state(
                text, e_dom=e_dom, e_val=e_val, target_intensity=e_val,
                tol=float(self.config.get("intensity_tol", 0.35) or 0.35),
            )
            if chk["action"] == "downgrade" and self._set_text(
                    result, postcheck.downgrade(text)):
                logger.info(f"[shihai-guard] 表达降级 dev={chk['deviation']}")
            tchk = postcheck.check_temporal_claim(text)
            if tchk["nudge"]:
                logger.info(f"[shihai-guard] 检出催促/追问线索：{tchk['hit']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[shihai-guard] postcheck failed: {type(exc).__name__} {exc}")
