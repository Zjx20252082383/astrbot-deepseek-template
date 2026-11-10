"""三层慢演化（论文 §17.7 的代码落点）。

L1 行为基因：只改 behaviors.json 里已经存在的参数（novelty / cooldown / anchor），
              不新增字段、不改 requires 的语义；
L2 策略先验：只调 PolicyEngine 的 epsilon_max（探索强度），不改 β、不改状态方程；
L3 叙事层：把“这周哪种行为被回应得多、哪种总没回音”写成一条观察笔记，
          只作为可读的自述，不进入 G_active、不进入锚点候选。

三条硬边界：
  1. 只写 evolution overlay（data/plugin_data/shihai/behavior_evolution.json），
     不直接改 behaviors.json，回滚就是删 overlay；
  2. 所有参数有界，单次扰动小（novelty ±0.04，cooldown ±10%，epsilon ±0.03）；
  3. 默认 24h 才评估一次，样本不足 3 次的行为不动。

它和 §11（θ 快更新）、§17.4（动作集内探索）是嵌套关系，不是替代：
§11 改阈值，§17.4 在固定动作集里探索，本模块才改动作集自身的参数先验。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

EVOLVE_INTERVAL_SEC = 24 * 3600
WINDOW_SEC = 7 * 86400
MIN_SAMPLES = 3
MAX_LOG_LINES = 2000
MAX_NOTES = 20

NOVELTY_MIN, NOVELTY_MAX = 0.05, 0.95
COOLDOWN_MIN, COOLDOWN_MAX = 300, 7200
EPS_MIN, EPS_MAX = 0.15, 0.60


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


class EvolutionEngine:
    """读行为注册表 + 行为回执，产出有界的慢演化 overlay。"""

    def __init__(self, data_dir: Path, enabled: bool = True,
                 interval_hours: float = 24.0):
        self.data_dir = Path(data_dir)
        self.enabled = bool(enabled)
        self.interval = max(3600.0, float(interval_hours) * 3600.0)
        self.overlay_path = self.data_dir / "behavior_evolution.json"
        self.log_path = self.data_dir / "evolution_log.jsonl"
        self.notes_path = self.data_dir / "evolution_notes.json"
        self.overlay: Dict[str, Any] = self._load_overlay()
        self._last_evolve = float(self.overlay.get("updated_at", 0) or 0)
        self._reply_keys: set[str] = set()

    # ---------- 持久化 ----------

    def _load_overlay(self) -> Dict[str, Any]:
        try:
            with open(self.overlay_path, "r", encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                raw.setdefault("genes", {})
                raw.setdefault("strategy", {})
                raw.setdefault("notes", [])
                return raw
        except (OSError, ValueError):
            pass
        return {"version": 1, "updated_at": 0, "genes": {}, "strategy": {},
                "notes": []}

    def _save_overlay(self):
        tmp = self.overlay_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.overlay, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.overlay_path)

    def _append_log(self, rec: Dict[str, Any]):
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 文件太大时截断到最近 MAX_LOG_LINES 行（低频，成本可忽略）
            if self.log_path.stat().st_size > 2_000_000:
                lines = self.log_path.read_text(encoding="utf-8").splitlines()
                self.log_path.write_text(
                    "\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _read_log(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        rows.append(rec)
        except OSError:
            pass
        return rows

    # ---------- 写入信号 ----------

    def record(self, action_id: str, ok: bool, kind: str = "",
               text: str = "", reason: str = "", ts: Optional[float] = None):
        if not self.enabled:
            return
        self._append_log({
            "event": "send",
            "ts": float(ts or time.time()),
            "action": str(action_id),
            "ok": bool(ok),
            "kind": str(kind),
            "text": str(text)[:80],
            "reason": str(reason)[:80],
        })

    def mark_replied(self, action_id: str, decision_ts: float):
        """主人对上一次主动消息有回应：只记一次，避免每 20s 重复打点。"""
        if not self.enabled or not action_id:
            return
        key = f"{action_id}:{round(float(decision_ts or 0), 3)}"
        if key in self._reply_keys:
            return
        self._reply_keys.add(key)
        self._append_log({
            "event": "reply",
            "ts": time.time(),
            "action": str(action_id),
        })

    # ---------- 应用到运行时 ----------

    def apply(self, behaviors: List[Dict[str, Any]]):
        genes = self.overlay.get("genes") or {}
        for act in behaviors:
            gene = genes.get(str(act.get("id", "")))
            if not isinstance(gene, dict):
                continue
            if "novelty" in gene:
                act["novelty"] = _clamp(float(gene["novelty"]), NOVELTY_MIN, NOVELTY_MAX)
            if "cooldown" in gene:
                act["cooldown"] = int(_clamp(float(gene["cooldown"]),
                                             COOLDOWN_MIN, COOLDOWN_MAX))
            if "anchor" in gene and isinstance(gene["anchor"], list):
                anchors = [str(a) for a in gene["anchor"] if str(a)]
                if anchors:
                    act["anchor"] = anchors

    def apply_strategy(self, policy):
        strategy = self.overlay.get("strategy") or {}
        if "epsilon_max" in strategy:
            policy.epsilon_max = _clamp(float(strategy["epsilon_max"]),
                                        EPS_MIN, EPS_MAX)

    def latest_note(self) -> str:
        notes = self.overlay.get("notes") or []
        return str(notes[-1]) if notes else ""

    # ---------- 三层评估 ----------

    def _group(self, now: float) -> Dict[str, Dict[str, Any]]:
        sends: Dict[str, Dict[str, Any]] = {}
        replies: Dict[str, int] = {}
        for rec in self._read_log():
            try:
                ts = float(rec.get("ts", 0) or 0)
            except (TypeError, ValueError):
                continue
            if now - ts > WINDOW_SEC:
                continue
            aid = str(rec.get("action", "") or "")
            if not aid:
                continue
            if rec.get("event") == "reply":
                replies[aid] = replies.get(aid, 0) + 1
                continue
            if rec.get("event") != "send":
                continue
            g = sends.setdefault(aid, {"n": 0, "ok": 0, "kinds": set()})
            g["n"] += 1
            g["ok"] += 1 if rec.get("ok") else 0
            g["kinds"].add(str(rec.get("kind", "") or ""))
        for aid, g in sends.items():
            g["reply"] = replies.get(aid, 0)
        return sends

    def maybe_evolve(self, now: float,
                     behaviors: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        if now - self._last_evolve < self.interval:
            return None
        stats = self._group(now)
        if not stats:
            self._last_evolve = now
            return None

        genes = self.overlay.setdefault("genes", {})
        by_id = {str(b.get("id", "")): b for b in behaviors}
        changed = False

        # L1 行为基因：只改 novelty / cooldown / anchor，全部有界
        for aid, s in stats.items():
            n = int(s["n"])
            if n < MIN_SAMPLES or aid not in by_id:
                continue
            act = by_id[aid]
            ok_rate = float(s["ok"]) / n
            reply_rate = min(1.0, float(s["reply"]) / n)
            gene = genes.get(aid) or {}
            nov = float(gene.get("novelty", act.get("novelty", 0.1)))
            cd = float(gene.get("cooldown",
                                act.get("cooldown", 1800)))
            # 有回应 → 这条行为值得保留，但降低新奇度（别为了新鲜而新鲜）；
            # 长期没回应 → 提高新奇度、缩短冷却，逼它换一种表达再试。
            if reply_rate >= 0.60 and ok_rate >= 0.70:
                nov -= 0.03
                cd *= 1.10
            elif reply_rate <= 0.25:
                nov += 0.04
                cd *= 0.90
            else:
                continue
            gene["novelty"] = round(_clamp(nov, NOVELTY_MIN, NOVELTY_MAX), 3)
            gene["cooldown"] = int(_clamp(cd, COOLDOWN_MIN, COOLDOWN_MAX))
            anchors = [str(a) for a in (gene.get("anchor")
                                        or act.get("anchor") or [])]
            if reply_rate <= 0.25 and "will" not in anchors:
                anchors.append("will")
            elif reply_rate >= 0.70 and "will" in anchors and len(anchors) > 1:
                anchors = [a for a in anchors if a != "will"]
            gene["anchor"] = anchors
            genes[aid] = gene
            changed = True

        # L2 策略先验：只看动作多样性，不碰 β
        total = sum(int(s["n"]) for s in stats.values())
        if total >= 5:
            distinct = len(stats)
            diversity = distinct / max(1, total)
            eps = float((self.overlay.get("strategy") or {}).get(
                "epsilon_max", 0.35))
            if diversity < 0.50:
                eps += 0.03
            elif diversity > 0.80:
                eps -= 0.02
            eps = _clamp(eps, EPS_MIN, EPS_MAX)
            strategy = self.overlay.setdefault("strategy", {})
            if abs(eps - float(strategy.get("epsilon_max", 0.35))) > 1e-6:
                strategy["epsilon_max"] = round(eps, 3)
                changed = True

        # L3 叙事层：一条人可读的观察，不进 G_active、不参与锚点候选
        ranked = sorted(stats.items(),
                        key=lambda kv: (kv[1]["reply"] / max(1, kv[1]["n"]),
                                        kv[1]["n"]), reverse=True)
        top = ranked[0]
        low = ranked[-1]
        note = (f"最近一周「{top[0]}」被回应 "
                f"{top[1]['reply']}/{top[1]['n']} 次；"
                f"「{low[0]}」只有 {low[1]['reply']}/{low[1]['n']} 次。"
                f"下一次优先给低回应行为换一种由头，而不是简单多喊几次。")
        notes = self.overlay.setdefault("notes", [])
        if not notes or notes[-1] != note:
            notes.append(note)
            del notes[:-MAX_NOTES]
            changed = True

        if changed:
            self.overlay["updated_at"] = float(now)
            self._last_evolve = float(now)
            self._save_overlay()
            self.apply(behaviors)
            self._append_log({
                "event": "evolve",
                "ts": float(now),
                "genes": len(genes),
                "strategy": self.overlay.get("strategy", {}),
                "note": note,
            })
        else:
            self._last_evolve = float(now)
        return self.overlay
