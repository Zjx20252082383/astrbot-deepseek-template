"""运行时不变式审计：把四条不变量做成可观测的计数器。

论文 §19（拟）的自检表：
    I1 时序单调   timeline.check_temporal_order
    I2 通道隔离   channels.audit_channels
    I3 表达归界   persona.scan_persona / check_m_fields
    I4 解释权唯一 persona.arbitrate（字段重叠即抛错）

每次生成前跑一遍，失败记 guard_trace.jsonl 一行；不抛异常中断对话，
只降级（去掉可疑字段而非整轮失败）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from . import channels, persona, timeline


class GuardAuditor:
    def __init__(self, trace_path: str | None = None):
        self.trace_path = trace_path
        self.counters = {"i1": 0, "i2": 0, "i3": 0, "i4": 0, "runs": 0}

    def audit(
        self,
        g_active: Sequence[Dict[str, Any]],
        anchors: Sequence[Dict[str, Any]],
        r_t: Sequence[Dict[str, Any]],
        persona_text: str,
        m_packet: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.counters["runs"] += 1
        res: Dict[str, Any] = {}

        i1 = timeline.check_temporal_order(r_t)
        res["i1"] = {"pass": i1}
        if not i1:
            self.counters["i1"] += 1

        leak = channels.audit_channels(g_active, anchors, r_t)
        i2 = all(v == 0 for v in leak.values())
        res["i2"] = {"pass": i2, "leak": leak}
        if not i2:
            self.counters["i2"] += 1

        i3a = persona.scan_persona(persona_text)
        i3b = persona.check_m_fields(m_packet)
        i3 = bool(i3a["pass"]) and bool(i3b["pass"])
        res["i3"] = {"pass": i3, "persona_violations": i3a["violations"],
                     "m_bad_keys": i3b["bad_keys"]}
        if not i3:
            self.counters["i3"] += 1

        i4 = not (set(m_packet) & {"style_params", "emotion", "release"})
        res["i4"] = {"pass": i4}
        if not i4:
            self.counters["i4"] += 1

        if self.trace_path:
            self._trace(res)
        return res

    def _trace(self, res: Dict[str, Any]) -> None:
        try:
            p = Path(self.trace_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": time.time(), "result": res, "counters": self.counters},
                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    def summary(self) -> Dict[str, Any]:
        runs = max(1, self.counters["runs"])
        return {
            "runs": self.counters["runs"],
            "violation_rate": {
                k: round(self.counters[k] / runs, 4) for k in ("i1", "i2", "i3", "i4")
            },
        }
