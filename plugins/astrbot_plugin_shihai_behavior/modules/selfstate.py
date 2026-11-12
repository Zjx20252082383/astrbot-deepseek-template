"""会话实例状态 z_s(s)（论文 6.9 第三分量；E 组的"近似分区"实现）。

为什么用 sidecar 而不是改 mind_state.json：
    6.14.3 明确 E 组优先以"按会话实例读写分片 + 检索门控"实现，不重建状态存储
    格式。心智核 astrbot_plugin_shihai 的 MindState 是全局单份、被 guard /
    dafeiyu / ai_cover 多方读取，改它的 schema 会连带改四个插件；因此在行为层
    落一份 sidecar `session_state.json`，按会话实例分片，格式独立、可整文件回滚。

分片键：会话实例 s。私聊/群聊各自一条；owner 私聊按 origin 分（两个 umo 就是
两个实例——这是现状，不是本模块要掩盖的东西）。

更新规则（6.9 的 z_s ← π(z_p, z_r(o))，落到可执行形式）：
    每来一条该会话的消息：
      1) 各维按 emotion.py 的 τ 衰减：e_d ← e_d·exp(−Δt/τ_d)
      2) 用词典代理效价 v 给一个刺激（工程先验，见 relational.valence_from_text）
         v > 0 → happy += 0.35v, love += 0.15v
         v < 0 → sad += 0.25|v|, angry += 0.25|v|
      3) 各维钳到 [0,1]
    会话空闲超过 IDLE_SEC 后，由 rollover() 交回关系层做一次 writeback（6.9 的
    z_r ← writeback(z_s, session end)）。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DIMS = ("happy", "sad", "angry", "fear", "love", "surprise")
# 与 astrbot_plugin_shihai/mind/emotion.py 的 DEFAULT_TAU 保持一致（论文 6.13.2 已引用）
TAU = {"happy": 6.0, "sad": 12.0, "angry": 2.0, "fear": 3.0,
       "love": 24.0, "surprise": 1.0}

GAIN_HAPPY = 0.35
GAIN_LOVE = 0.15
GAIN_SAD = 0.25
GAIN_ANGRY = 0.25
IDLE_SEC = 2 * 3600      # 会话实例"结束"的判据（6.9 writeback 的触发条件）

_LABEL_HI = ("开心", "高兴", "兴奋", "热络")
_LABEL_LO = ("沉", "低", "闷", "累")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class SessionStore:
    """按会话实例分片的 z_s；只保存情绪、轮次、效价与时间，不保存原文。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.items: Dict[str, Dict[str, Any]] = {}
        self._last_save: float = 0.0
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        rows = data.get("items") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            for k, v in rows.items():
                if not isinstance(v, dict):
                    continue
                e = v.get("e") or {}
                self.items[str(k)] = {
                    "obj": str(v.get("obj", "owner")),
                    "e": {d: _clamp01(float(e.get(d, 0.0) or 0.0)) for d in DIMS},
                    "turns": int(v.get("turns", 0) or 0),
                    "v_sum": float(v.get("v_sum", 0.0) or 0.0),
                    "v_n": int(v.get("v_n", 0) or 0),
                    "first_at": float(v.get("first_at", 0.0) or 0.0),
                    "last_at": float(v.get("last_at", 0.0) or 0.0),
                    "topic": str(v.get("topic", "") or "")[:40],
                }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"items": self.items}, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, str(self.path))
            self._last_save = time.time()
        except OSError:
            pass

    # ---------- 更新 ----------
    def touch(self, key: str, obj: str, text: str, valence: float,
              now: Optional[float] = None) -> Dict[str, Any]:
        now = float(now or time.time())
        key = str(key or obj or "owner")
        row = self.items.get(key)
        if row is None:
            row = {"obj": str(obj or "owner"),
                   "e": {d: 0.0 for d in DIMS}, "turns": 0,
                   "v_sum": 0.0, "v_n": 0, "first_at": now, "last_at": now,
                   "topic": ""}
            self.items[key] = row
        # 1) 按 τ 衰减到此刻
        dt_h = max(0.0, (now - float(row.get("last_at") or now)) / 3600.0)
        for d in DIMS:
            row["e"][d] = _clamp01(float(row["e"].get(d, 0.0))
                                   * math.exp(-dt_h / max(0.1, TAU[d])))
        # 2) 刺激
        v = max(-1.0, min(1.0, float(valence)))
        if v > 0:
            row["e"]["happy"] = _clamp01(row["e"]["happy"] + GAIN_HAPPY * v)
            row["e"]["love"] = _clamp01(row["e"]["love"] + GAIN_LOVE * v)
        elif v < 0:
            row["e"]["sad"] = _clamp01(row["e"]["sad"] + GAIN_SAD * (-v))
            row["e"]["angry"] = _clamp01(row["e"]["angry"] + GAIN_ANGRY * (-v))
        # 3) 计数与话题
        row["turns"] = int(row.get("turns") or 0) + 1
        row["v_sum"] = float(row.get("v_sum") or 0.0) + v
        row["v_n"] = int(row.get("v_n") or 0) + 1
        row["last_at"] = now
        if text:
            row["topic"] = str(text).replace("\n", " ")[:40]
        # 5 min 节流落盘：z_s 是"重启后还要在"的状态，不能只活在内存里
        if now - float(self._last_save or 0) > 300:
            self.save()
        return row

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self.items.get(str(key or ""))

    def mean_valence(self, key: str) -> float:
        row = self.items.get(str(key or "")) or {}
        n = int(row.get("v_n") or 0)
        if n <= 0:
            return 0.0
        return max(-1.0, min(1.0, float(row.get("v_sum") or 0.0) / n))

    # ---------- 会话结束回写 ----------
    def rollover(self, now: Optional[float] = None,
                 idle: float = IDLE_SEC) -> List[Dict[str, Any]]:
        """返回空闲超时的分片（供调用方 writeback 到关系层），并把它们归档。"""
        now = float(now or time.time())
        out: List[Dict[str, Any]] = []
        for key, row in list(self.items.items()):
            if now - float(row.get("last_at") or now) < idle:
                continue
            if int(row.get("turns") or 0) <= 0:
                self.items.pop(key, None)
                continue
            out.append({"key": key, "obj": str(row.get("obj", "owner")),
                        "valence": self.mean_valence(key),
                        "turns": int(row.get("turns") or 0)})
            self.items.pop(key, None)
        if out:
            self.save()
        return out


def _top_dim(row: Dict[str, Any]) -> tuple[str, float]:
    e = row.get("e") or {}
    if not e:
        return "calm", 0.0
    d = max(DIMS, key=lambda k: float(e.get(k, 0.0)))
    return d, float(e.get(d, 0.0))


_DIM_WORD = {"happy": "轻快", "sad": "低落", "angry": "有点上火",
             "fear": "不安", "love": "亲近", "surprise": "意外"}


def familiarity_word(r_o: Optional[float]) -> str:
    """关系强度 → 定性词（6-21 的数值不进提示词，避免她报数字）。"""
    r = float(r_o or 0.0)
    if r < 0.30:
        return "基本不熟"
    if r < 0.45:
        return "只是眼熟"
    if r < 0.62:
        return "聊过几次"
    return "还算熟"


def session_block(row: Optional[Dict[str, Any]], r_o: Optional[float]) -> str:
    """非主人会话的自我状态块：只有"此刻的我"和"跟这个人多熟"，无主人任何内容。

    刻意不包含：z_p 的具体数值、G_active、fire_pending、last_decision、共享层记忆。
    """
    if not row:
        return ""
    dim, strength = _top_dim(row)
    if strength < 0.12:
        mood = "情绪很平，没什么起伏"
    else:
        mood = "底色偏%s" % _DIM_WORD.get(dim, "平稳")
    return ("（你此刻的状态：%s；跟眼前这个人%s。回应时按这个基调说话，"
            "不要提任何别人的私事。）" % (mood, familiarity_word(r_o)))
