"""熟人名单：她自己提议，主人拍板。

为什么不让她自己定 ——
"谁是我弟" 是一个**可核验谓词**：只有鑫鑫知道，且验证方式是鑫鑫说一句。
她能拿到的只有两样东西：
  ① 对方说了什么（"我是他弟"）—— 任何人可冒充，不是证据；
  ② 这个号在本机历史上出现过几次、聊了多久 —— 这是"号→关系"的客观痕迹，
     不可伪造，但只能证明"这个人确实反复来过"，不能证明"他是谁"。

所以本模块把两件事彻底分开：
  · observe() 只统计 ②（次数 / 跨度），据此生成**提议**；
  · decide() 只在主人通道被调用，且必须由主人一句话触发（见 parse_decision 的窄正则）。
她永远没有 approve 的代码路径：本模块不提供自动批准，也没有阈值能自己翻过去。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROPOSE_MIN_MSGS = 3            # 至少 3 条真实消息
PROPOSE_MIN_SPAN = 6 * 3600     # 且跨度 ≥ 6 小时（同一次会话里刷 3 句不算）
MAX_AGE = 45 * 86400            # 45 天没动静的提议自动过期，不再打扰

_APPROVE_RX = (
    re.compile(r"熟人\s*(?:qq:|QQ)?\s*(\d{5,12})\s*(?:可以|行|加上|加进|通过|批准|算|好)"),
    re.compile(r"(?:qq:|QQ)?\s*(\d{5,12})\s*(?:记成|设为|加进|加入|放进)\s*熟人"),
)
_REJECT_RX = re.compile(r"熟人\s*(?:qq:|QQ)?\s*(\d{5,12})\s*(?:不要|不行|不加|不用|拒绝|先别)")


class Roster:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.kf_path = self.base / "known_friends.json"
        self.kc_path = self.base / "known_candidates.json"
        # 她自己的号（bot_qid）与主人号永远不进候选：否则她会提议"把我自己记成熟人"
        self.exclude: set[str] = set()
        self.items: Dict[str, Dict[str, Any]] = self._load()

    def set_exclude(self, ids) -> None:
        for x in ids or []:
            s = str(x).strip()
            if s:
                self.exclude.add(s[3:] if s.startswith("qq:") else s)

    # ---------- 存读 ----------
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.kc_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def save(self) -> None:
        try:
            self.kc_path.write_text(
                json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def known(self) -> List[str]:
        try:
            raw = json.loads(self.kf_path.read_text(encoding="utf-8-sig"))
            seq = raw if isinstance(raw, list) else (
                raw.get("friends") if isinstance(raw, dict) else [])
            out = []
            for x in seq or []:
                s = str(x).strip()
                if s:
                    out.append(s[3:] if s.startswith("qq:") else s)
            return out
        except Exception:  # noqa: BLE001
            return []

    # ---------- 观察（只认"号来过几次"，不认自称） ----------
    def observe(self, qid: str, text: str, name: str = "",
                now: Optional[float] = None) -> None:
        q = str(qid or "").strip()
        if not q or q in self.exclude:
            return
        now = float(now or time.time())
        rec = self.items.get(q)
        if rec is None:
            rec = {"msgs": 0, "first": now, "last": now,
                   "status": "pending", "proposed": 0, "samples": [], "name": ""}
            self.items[q] = rec
        nm = str(name or "").strip().replace("\n", " ")[:16]
        if nm:
            rec["name"] = nm
        rec["msgs"] = int(rec.get("msgs", 0)) + 1
        rec["first"] = float(rec.get("first") or now)
        rec["last"] = now
        # 只留最近 3 条短样本，给主人判断时看语气，不做任何身份推断
        sm = list(rec.get("samples") or [])
        s = str(text or "").strip()[:40]
        if s and (not sm or sm[-1] != s):
            sm = (sm + [s])[-3:]
        rec["samples"] = sm
        self.save()

    def projected(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """够格提议、且还没被处理过的号。"""
        now = float(now or time.time())
        out: List[Dict[str, Any]] = []
        for q, rec in self.items.items():
            if q in self.exclude:
                rec["status"] = "self"
                continue
            if rec.get("status") not in ("pending", "proposed"):
                continue
            if q in set(self.known()):
                rec["status"] = "already"
                continue
            if now - float(rec.get("last") or 0) > MAX_AGE:
                rec["status"] = "expired"
                continue
            span = float(rec.get("last") or 0) - float(rec.get("first") or 0)
            if int(rec.get("msgs", 0)) >= PROPOSE_MIN_MSGS and span >= PROPOSE_MIN_SPAN:
                if not rec.get("proposed"):
                    rec["proposed"] = now
                rec["status"] = "proposed"
                out.append({"qid": q, **rec})
        if out:
            self.save()
        return out

    # ---------- 给主人的一句话（只在主人通道出现） ----------
    def brief(self, now: Optional[float] = None, max_n: int = 3) -> str:
        props = self.projected(now)
        if not props:
            return ""
        props.sort(key=lambda r: -int(r.get("msgs", 0)))
        lines = ["【有个号想让你认一下】"]
        for r in props[:max_n]:
            days = max(1, int((float(r.get("last") or 0) - float(r.get("first") or 0)) // 86400))
            nm = str(r.get("name") or "").strip()
            who = f"QQ{r['qid']}" + (f"（昵称「{nm}」）" if nm else "")
            lines.append(
                f"- {who}：聊过 {r.get('msgs')} 次（{days} 天里），"
                f"最近说的像「{(r.get('samples') or [''])[-1][:24]}」")
        lines.append("要不要记成熟人？回「熟人 %s 可以」或「熟人 %s 不要」就行。"
                     % (props[0]["qid"], props[0]["qid"]))
        return "\n".join(lines)

    # ---------- 决定（只由主人触发） ----------
    def parse_decision(self, text: str) -> tuple[Optional[str], Optional[bool]]:
        t = str(text or "")
        for rx in _APPROVE_RX:
            m = rx.search(t)
            if m:
                return m.group(1), True
        m = _REJECT_RX.search(t)
        if m:
            return m.group(1), False
        return None, None

    def decide(self, qid: str, approve: bool) -> bool:
        """这是唯一改名单的地方。调用方必须先确认 sender 是主人。"""
        q = str(qid or "").strip()
        if not q or q in self.exclude:
            return False
        if approve:
            friends = self.known()
            if q not in friends:
                friends.append(q)
                payload = {"friends": friends}
                try:  # 保留原有备注
                    old = json.loads(self.kf_path.read_text(encoding="utf-8-sig"))
                    if isinstance(old, dict) and isinstance(old.get("备注"), dict):
                        payload["备注"] = old["备注"]
                except Exception:  # noqa: BLE001
                    pass
                self.kf_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        rec = self.items.get(q)
        if rec is not None:
            rec["status"] = "approved" if approve else "rejected"
            rec["decided"] = time.time()
        self.save()
        return True
