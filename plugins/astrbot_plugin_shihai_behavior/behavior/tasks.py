"""生活任务层：把短信里的“待办”接成可确认、可排期、可回执的任务状态机。

设计边界（与论文 §15 一致）：
  · 任务提醒走确定性即时通道，不进入念头阈值 u(t) → 不会被“想不想开口”延迟；
  · 任务层仍读 z(t) 快照，用情绪主导维与念头占比决定语气，但不改变触发时刻；
  · 短信原文、命中字段、情境依据（课表/关门/步行）全部落档，提醒理由可追溯；
  · 本模块不依赖 AstrBot，便于单测；发送函数由 main.py 注入。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

DEFAULT_CONTEXT: Dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "class_end": "16:00",
    "station_close": "18:00",
    "walk_minutes": 8,
    "pickup_location": "菜鸟驿站",
    "quiet_hours": [23, 7],
    "remind_before_close_minutes": 90,
}

STATUS_LABELS = {
    "pending_confirm": "待确认",
    "awaiting_confirm": "待确认",
    "planned": "已排期",
    "reminded": "已提醒",
    "done": "已取/已办",
    "archived": "已归档",
    "ignored": "已忽略",
    "deferred": "已延后",
    "expired": "已过期",
}

ACTIVE_STATUSES = ("pending_confirm", "awaiting_confirm", "planned",
                   "reminded", "deferred")

TASK_REPLIES = {
    "确认": "confirm", "确认取快递": "confirm",
    "已取": "done", "已取快递": "done", "快递已取": "done", "取了快递": "done",
    "忽略": "ignore", "忽略快递": "ignore", "不取了": "ignore",
    "延后": "defer", "延后取快递": "defer",
}
TASK_ID_RX = re.compile(r"tk_[0-9a-f]{10}")
DEFER_SECONDS = 3600


def _now_hm(now: float) -> str:
    return time.strftime("%H:%M", time.localtime(now))


def _parse_hm(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M")
    except (TypeError, ValueError):
        return None


def _today_at(now: float, hm: str) -> Optional[datetime]:
    parsed = _parse_hm(hm)
    if parsed is None:
        return None
    base = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    return base.replace(hour=parsed.hour, minute=parsed.minute)


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _short(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


class TaskStore:
    """tasks.json 的唯一读写入口；坏文件不覆盖，回退空表。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: Dict[str, Any] = {"version": 1, "tasks": []}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict) and isinstance(raw.get("tasks"), list):
                    self.data = raw
                elif isinstance(raw, list):
                    self.data = {"version": 1, "tasks": raw}
        except (OSError, ValueError, TypeError):
            self.data = {"version": 1, "tasks": []}

    def save(self) -> None:
        _atomic_json(self.path, self.data)

    @property
    def tasks(self) -> List[Dict[str, Any]]:
        return self.data.setdefault("tasks", [])

    def find(self, task_id: str) -> Optional[Dict[str, Any]]:
        task_id = str(task_id or "").strip()
        for task in self.tasks:
            if str(task.get("id", "")) == task_id:
                return task
        return None


class TaskEngine:
    """短信 → 任务卡 → 确认/排期 → 提醒 → 已取 → 归档。"""

    def __init__(
        self,
        data_dir: Path,
        send_text: Optional[Callable[[str], Awaitable[bool]]] = None,
        sms_records_path: Optional[Path] = None,
        context_path: Optional[Path] = None,
    ):
        self.data_dir = Path(data_dir)
        self.store = TaskStore(self.data_dir / "tasks.json")
        self.events_path = self.data_dir / "task_events.jsonl"
        self.context_path = Path(context_path) if context_path else (
            self.data_dir / "task_context.json")
        self.sms_records_path = Path(sms_records_path) if sms_records_path else None
        self.send_text = send_text
        self.context = self._load_context()
        self._ingested: set[str] = set()
        for task in self.store.tasks:
            ref = str((task.get("source") or {}).get("ref", ""))
            if ref:
                self._ingested.add(ref)

    # ---------- 配置与落档 ----------

    def _load_context(self) -> Dict[str, Any]:
        data = dict(DEFAULT_CONTEXT)
        try:
            if self.context_path.exists():
                raw = json.loads(self.context_path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items() if k in data or k == "source"})
        except (OSError, ValueError, TypeError):
            pass
        try:
            if not self.context_path.exists():
                _atomic_json(self.context_path, data)
        except OSError:
            pass
        return data

    def set_context(self, **patch: Any) -> Dict[str, Any]:
        for key, value in patch.items():
            if key in DEFAULT_CONTEXT and value not in (None, ""):
                self.context[key] = value
        self.context["source"] = "user_configured"
        _atomic_json(self.context_path, self.context)
        return dict(self.context)

    def _event(self, task: Dict[str, Any], kind: str, note: str = "") -> None:
        row = {
            "ts": time.time(),
            "task_id": task.get("id", ""),
            "kind": kind,
            "status": task.get("status", ""),
            "note": _short(note, 240),
        }
        try:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 短信摄入 ----------

    def ingest_sms(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        if not self.sms_records_path or not self.sms_records_path.exists():
            return []
        try:
            records = json.loads(self.sms_records_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            return []
        if not isinstance(records, list):
            return []
        created: List[Dict[str, Any]] = []
        for rec in records:
            if not isinstance(rec, dict) or not rec.get("express"):
                continue
            ref = str(rec.get("dedup") or hashlib.md5(
                f"{rec.get('sender')}|{rec.get('text')}|{rec.get('ts')}".encode()).hexdigest())
            if ref in self._ingested:
                continue
            task = self._from_express(rec, ref)
            if task is None:
                continue
            self.store.tasks.append(task)
            self._ingested.add(ref)
            self._event(task, "discovered", "短信解析命中快递字段")
            created.append(task)
        if created:
            self.store.save()
        return created

    def _from_express(self, rec: Dict[str, Any], ref: str) -> Optional[Dict[str, Any]]:
        exp = rec.get("express") or {}
        if not isinstance(exp, dict):
            return None
        carrier = _short(exp.get("carrier", "快递"), 40)
        pickup = _short(exp.get("pickup", ""), 60)
        raw_text = str(rec.get("text") or exp.get("summary") or "")
        m_place = re.search(r"(菜鸟驿站[^，,。；;\s]*)", raw_text)
        place = _short(exp.get("location") or exp.get("place")
                       or (m_place.group(1) if m_place else ""), 80)
        location = _short(place or self.context.get("pickup_location", ""), 80)
        title = f"取快递：{carrier}" + (f"（{pickup}）" if pickup else "")
        now = float(rec.get("ts") or time.time())
        task_id = "tk_" + hashlib.sha1(ref.encode("utf-8")).hexdigest()[:10]
        return {
            "id": task_id,
            "kind": "express_pickup",
            "title": title,
            "status": "pending_confirm",
            "created_at": now,
            "updated_at": now,
            "source": {
                "channel": "sms",
                "ref": ref,
                "from": _short(rec.get("sender", ""), 40),
                "time": str(rec.get("time") or ""),
                "raw": _short(rec.get("text", ""), 220),
            },
            "evidence": {
                "carrier": carrier,
                "pickup": pickup,
                "waybill": _short(exp.get("waybill", ""), 80),
                "phone_tail": _short(exp.get("phone_tail", ""), 20),
                "status": _short(exp.get("status", ""), 80),
                "place": place or location,
            },
            "location": location or place,
            "window_start": None,
            "window_end": None,
            "remind_at": None,
            "defer_until": None,
            "last_notify_at": 0.0,
            "history": [{"at": now, "from": "sms", "to": "pending_confirm",
                         "by": "system", "note": "短信解析为快递待办"}],
        }

    # ---------- 状态机 ----------

    def _transition(self, task: Dict[str, Any], to: str, by: str, note: str = "") -> None:
        old = str(task.get("status", ""))
        task["status"] = to
        task["updated_at"] = time.time()
        task.setdefault("history", []).append({
            "at": task["updated_at"], "from": old, "to": to, "by": by,
            "note": _short(note, 160),
        })
        self._event(task, "transition", f"{old}→{to} {note}")

    def _plan_window(self, now: float, delay: int = 0) -> Optional[Dict[str, Any]]:
        class_end = _today_at(now, str(self.context.get("class_end", "16:00")))
        close = _today_at(now, str(self.context.get("station_close", "18:00")))
        try:
            walk = int(self.context.get("walk_minutes", 8))
        except (TypeError, ValueError, OverflowError):
            return None
        if class_end is None or close is None or walk <= 0:
            return None
        return {
            "start": max(now + delay, class_end.timestamp()),
            "end": close.timestamp(),
            "walk_minutes": walk,
            "class_end": class_end.timestamp(),
        }

    @staticmethod
    def _set_window(task: Dict[str, Any], window: Dict[str, Any], deferred: bool = False) -> str:
        start, end, walk = window["start"], window["end"], window["walk_minutes"]
        reason = f"{_now_hm(window['class_end'])} 下课，驿站 {_now_hm(end)} 关门，步行 {walk} 分钟"
        if start + walk * 60 >= end:
            start = None
        else:
            reason += f"，建议 {_now_hm(start)} 出发（预计 {_now_hm(start + walk * 60)} 到达）"
        task.update(window_start=start, window_end=end, walk_minutes=walk,
                    remind_at=start, defer_until=start if deferred else None, plan_reason=reason)
        return reason

    def _schedule(self, task: Dict[str, Any], now: float, by: str, delay: int = 0) -> str:
        window = self._plan_window(now, delay)
        if window is None:
            return "请先检查调度设置里的下课时间、驿站关门时间和步行分钟数。"
        reason = self._set_window(task, window, deferred=bool(delay))
        if task["remind_at"] is None:
            if task.get("status") != "expired":
                self._transition(task, "expired", "system", "今天无法在关门前到达")
            self.store.save()
            return f"{reason}，今天已来不及。明天可回复「确认 {task['id']}」重新排期。"
        self._transition(task, "deferred" if delay else "planned", by, reason)
        self.store.save()
        if delay:
            return f"已延后至 {_now_hm(task['remind_at'])}，到点我会提醒你。"
        return self.format_planned(task)

    def plan(self, task: Dict[str, Any], now: Optional[float] = None,
             by: str = "user") -> str:
        return self._schedule(task, time.time() if now is None else now, by)

    def mark_done(self, task: Dict[str, Any], now: Optional[float] = None,
                  by: str = "user") -> str:
        now = float(now or time.time())
        self._transition(task, "done", by, "用户确认已取/已办")
        task["completed_at"] = now
        self._transition(task, "archived", "system", "完成后归档")
        self.store.save()
        return f"已归档：{task.get('title', '')}。这件事不再提醒。"

    def mark_ignored(self, task: Dict[str, Any], now: Optional[float] = None,
                     by: str = "user") -> str:
        now = float(now or time.time())
        self._transition(task, "ignored", by, "用户选择忽略")
        self.store.save()
        return f"已忽略：{task.get('title', '')}。需要时再说一声。"

    def defer(self, task: Dict[str, Any], now: Optional[float] = None,
              by: str = "user") -> str:
        return self._schedule(task, time.time() if now is None else now, by, DEFER_SECONDS)

    # ---------- 提醒 ----------

    def _mood_prefix(self, state: Dict[str, Any]) -> str:
        mood = str(state.get("e_dom", "") or "")
        return {
            "happy": "我心情正好，",
            "love": "我正惦记着你，",
            "sad": "我有点担心你，",
            "angry": "我压着一点急，",
            "fear": "我怕你忘了，",
            "surprise": "我刚想起来，",
        }.get(mood, "")

    def _is_quiet(self, now: float) -> bool:
        quiet = self.context.get("quiet_hours") or []
        try:
            start, end = int(quiet[0]), int(quiet[1])
        except (TypeError, ValueError, IndexError):
            return False
        hour = time.localtime(now).tm_hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _refresh_legacy_window(self, task: Dict[str, Any], now: float) -> None:
        """按原排期的日期重新核对旧记录，避免把过期提醒顺延到今天。"""
        deferred = task["status"] == "deferred"
        delay = DEFER_SECONDS if deferred else 0
        reminder = task.get("remind_at")
        reference = float(reminder) - delay if reminder else float(task.get("updated_at") or now)
        window = self._plan_window(reference, delay)
        if window is None:
            task["remind_at"] = None
            return
        if task.get("window_end"):
            window["end"] = min(window["end"], float(task["window_end"]))
        self._set_window(task, window, deferred)

    async def tick(self, now: Optional[float] = None,
                   state: Optional[Dict[str, Any]] = None) -> List[str]:
        now = time.time() if now is None else now
        state = state or {}
        self.ingest_sms(now)
        sent: List[str] = []
        due = []
        window_changed = False
        for task in self.store.tasks:
            status = task.get("status")
            if status == "reminded":
                end = task.get("window_end")
                # 已经发过提醒的任务保留一小时，便于用户补回已取回执。
                if end and now > end + 3600:
                    self._transition(task, "expired", "system", "任务窗口已过")
                    window_changed = True
                continue
            if status not in ("planned", "deferred"):
                continue
            if "walk_minutes" not in task:
                self._refresh_legacy_window(task, now)
                window_changed = True
            reminder = task.get("remind_at")
            end = task.get("window_end")
            walk = task.get("walk_minutes", 0)
            if reminder is None or not end or now + walk * 60 >= end:
                self._transition(task, "expired", "system", "已无法在关门前到达")
                window_changed = True
            elif reminder <= now:
                due.append(task)
        if window_changed:
            self.store.save()

        # 先处理到点提醒，避免发送新任务卡的等待消耗剩余取件时间。
        if due and not self._is_quiet(now):
            task = min(due, key=lambda row: row["remind_at"])
            scheduled = (task["status"], task["remind_at"])
            if await self._send(self.format_reminder(task, state)):
                if (task["status"], task.get("remind_at")) == scheduled:
                    self._transition(task, "reminded", "system", "到点提醒")
                    task["last_notify_at"] = now
                    self.store.save()
                sent.append(task["id"])

        # 新任务先发“待确认”卡；重启后未发出的待确认任务也补发，同一轮最多一张
        pending_cards = [t for t in self.store.tasks
                         if t.get("status") == "pending_confirm"
                         and not float(t.get("last_notify_at") or 0)]
        if pending_cards:
            task = max(pending_cards, key=lambda t: float(t.get("created_at", 0)))
            if await self._send_card(task, state):
                sent.append(task.get("id", ""))
        self.store.save()
        return sent

    async def _send(self, text: str) -> bool:
        if not text or not self.send_text:
            return False
        try:
            result = await self.send_text(text)
            return bool(result)
        except Exception:
            return False

    async def _send_card(self, task: Dict[str, Any], state: Dict[str, Any]) -> bool:
        text = self.format_card(task, state)
        ok = await self._send(text)
        if ok and task["status"] == "pending_confirm":
            self._transition(task, "awaiting_confirm", "system", "已发任务卡")
            task["last_notify_at"] = time.time()
            self.store.save()
        return ok

    # ---------- 文本 ----------

    def format_card(self, task: Dict[str, Any], state: Dict[str, Any] | None = None) -> str:
        ev = task.get("evidence") or {}
        bits = [f"有一件要你点头的事：{task.get('title', '待办')}"]
        if ev.get("place"):
            bits.append(f"地点：{ev['place']}")
        if ev.get("pickup"):
            bits.append(f"取件码：{ev['pickup']}")
        if ev.get("status"):
            bits.append(f"状态：{ev['status']}")
        # 短信原文只落档（source.raw）供追溯，不再贴进任务卡——避免把原始日志暴露到聊天里
        bits.append(f"回复「确认 {task['id']}」排期，回复「忽略 {task['id']}」取消提醒。")
        return "\n".join(bits)

    def format_planned(self, task: Dict[str, Any]) -> str:
        reason = task.get("plan_reason") or "按当前时间安排"
        return (f"排好了：{task.get('title', '')}\n"
                f"{reason}。到点我叫你，取完回复「已取 {task['id']}」归档。")

    def format_reminder(self, task: Dict[str, Any],
                        state: Optional[Dict[str, Any]] = None) -> str:
        state = state or {}
        prefix = self._mood_prefix(state)
        reason = task.get("plan_reason") or "现在出发比较顺路"
        ev = task.get("evidence") or {}
        line = f"{prefix}该去取快递了：{task.get('title', '')}"
        if ev.get("pickup"):
            line += f"，取件码 {ev['pickup']}"
        line += f"。{reason}。取完回复「已取 {task['id']}」归档。"
        return line

    def format_list(self) -> str:
        active = [t for t in self.store.tasks if t.get("status") in ACTIVE_STATUSES]
        if not active:
            return "现在没有待办任务。"
        lines = ["【生活任务】"]
        for task in sorted(active, key=lambda t: float(t.get("created_at", 0)),
                           reverse=True)[:8]:
            label = STATUS_LABELS.get(str(task.get("status")), task.get("status"))
            line = f"- {task.get('id')}｜{label}｜{task.get('title', '')}"
            if task.get("plan_reason"):
                line += f"｜{task['plan_reason']}"
            lines.append(line)
        return "\n".join(lines)

    # ---------- 用户回执 ----------

    def handle_text(self, text: str, now: Optional[float] = None,
                    by: str = "owner", *, expected_action: str = "",
                    expected_task_id: str = "") -> Optional[str]:
        parts = str(text or "").strip().rstrip("。！!").split()
        if not 1 <= len(parts) <= 2:
            return None
        action = TASK_REPLIES.get(parts[0])
        if action is None or (expected_action and action != expected_action):
            return None
        if len(parts) == 2:
            if not TASK_ID_RX.fullmatch(parts[1]):
                return None
            task = self.store.find(parts[1])
            if task is None:
                return "找不到这个任务，请核对任务卡上的编号。"
        else:
            active = [t for t in self.store.tasks if t.get("status") in ACTIVE_STATUSES]
            if not active:
                return None
            if len(active) > 1:
                return f"现在有多条待办，请回复「{parts[0]} 任务编号」，编号见各自的任务卡。"
            task = active[0]
        if expected_task_id and task["id"] != expected_task_id.strip():
            return None
        status = task["status"]
        if action == "confirm" and status in ("pending_confirm", "awaiting_confirm", "deferred", "expired"):
            return self.plan(task, now, by)
        if status not in ACTIVE_STATUSES or action == "confirm":
            return f"当前状态为「{STATUS_LABELS.get(status, status)}」。"
        if action == "done":
            return self.mark_done(task, now, by)
        if action == "ignore":
            return self.mark_ignored(task, now, by)
        return self.defer(task, now, by)
