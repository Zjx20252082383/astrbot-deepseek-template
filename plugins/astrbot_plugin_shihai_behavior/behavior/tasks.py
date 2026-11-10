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
from datetime import datetime, timedelta
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

CONFIRM_RX = re.compile(
    r"(确认|安排|排一下|帮我取|帮我去|顺路取|我去取|知道了|收到|可以|行)")
IGNORE_RX = re.compile(r"(忽略|不用|不取|取消|算了|先不管)")
DONE_RX = re.compile(r"(已取|取了|拿到|取到|办好了|办完|完成)")
DEFER_RX = re.compile(r"(延后|晚点|等会|下午再|晚上再|明天再)")
TASK_WORD_RX = re.compile(r"(快递|取件|包裹|任务|待办|驿站|取一下)")


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

    def latest_active(self) -> Optional[Dict[str, Any]]:
        active = [t for t in self.tasks if t.get("status") in ACTIVE_STATUSES]
        return max(active, key=lambda t: float(t.get("created_at", 0))) if active else None


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

    def _plan_window(self, now: float) -> Dict[str, Any]:
        class_end = _today_at(now, str(self.context.get("class_end", "16:00")))
        close = _today_at(now, str(self.context.get("station_close", "18:00")))
        walk = max(1, int(self.context.get("walk_minutes", 8) or 8))
        if class_end is None or close is None:
            return {"start": now, "end": now + 2 * 3600, "reason": "按当前时间安排"}
        if close <= class_end:
            close += timedelta(days=1)
        start_dt = class_end - timedelta(minutes=10)
        if start_dt < datetime.fromtimestamp(now):
            start_dt = datetime.fromtimestamp(now) + timedelta(minutes=5)
        if start_dt >= close:
            start_dt = close - timedelta(minutes=walk)
        start = start_dt.timestamp()
        end = close.timestamp()
        reason = (f"{class_end.strftime('%H:%M')} 下课，"
                  f"驿站 {close.strftime('%H:%M')} 关门，"
                  f"步行 {walk} 分钟，"
                  f"{start_dt.strftime('%H:%M')} 出发最顺路")
        return {"start": start, "end": end, "reason": reason}

    def plan(self, task: Dict[str, Any], now: Optional[float] = None,
             by: str = "user") -> str:
        now = float(now or time.time())
        window = self._plan_window(now)
        if float(window.get("end") or 0) < now:
            self._transition(task, "expired", "system", "计划时已过关门时间")
            self.store.save()
            return f"今天这个时间点来不及了：{task.get('title', '')}。明天再取，或回我「延后」。"
        task["window_start"] = window["start"]
        task["window_end"] = window["end"]
        task["remind_at"] = max(now, window["start"] - 10 * 60)
        task["plan_reason"] = window["reason"]
        self._transition(task, "planned", by, window["reason"])
        self.store.save()
        return self.format_planned(task)

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
        now = float(now or time.time())
        task["defer_until"] = now + 60 * 60
        task["remind_at"] = task["defer_until"]
        self._transition(task, "deferred", by, "延后 1 小时")
        self.store.save()
        return f"先记着：{task.get('title', '')}。一小时后我再提醒你。"

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

    async def tick(self, now: Optional[float] = None,
                   state: Optional[Dict[str, Any]] = None) -> List[str]:
        now = float(now or time.time())
        state = state or {}
        created = self.ingest_sms(now)
        sent: List[str] = []
        # 新任务先发“待确认”卡；重启后未发出的待确认任务也补发，同一轮最多一张
        pending_cards = [t for t in self.store.tasks
                         if t.get("status") == "pending_confirm"
                         and not float(t.get("last_notify_at") or 0)]
        if pending_cards:
            task = max(pending_cards, key=lambda t: float(t.get("created_at", 0)))
            if await self._send_card(task, state):
                sent.append(task.get("id", ""))
        # 已排期/延后到点的任务：确定性即时提醒，不看 u(t) 阈值
        due = [t for t in self.store.tasks
               if t.get("status") in ("planned", "deferred")
               and float(t.get("remind_at") or 0) <= now]
        if due:
            due.sort(key=lambda t: float(t.get("remind_at") or 0))
            task = due[0]
            if not self._is_quiet(now):
                text = self.format_reminder(task, state)
                if await self._send(text):
                    self._transition(task, "reminded", "system", "到点提醒")
                    task["last_notify_at"] = now
                    self.store.save()
                    sent.append(task.get("id", ""))
        # 窗口已过：标记过期，避免第二天继续提醒
        for task in self.store.tasks:
            if task.get("status") in ("planned", "reminded", "deferred"):
                end = float(task.get("window_end") or 0)
                if end and now > end + 3600:
                    self._transition(task, "expired", "system", "任务窗口已过")
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
        if ok:
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
        bits.append("回我「确认」我就排时间；回「忽略」就不提醒。")
        return "\n".join(bits)

    def format_planned(self, task: Dict[str, Any]) -> str:
        reason = task.get("plan_reason") or "按当前时间安排"
        return (f"排好了：{task.get('title', '')}\n"
                f"{reason}。到点我叫你；取完回我「已取」就归档。")

    def format_reminder(self, task: Dict[str, Any],
                        state: Optional[Dict[str, Any]] = None) -> str:
        state = state or {}
        prefix = self._mood_prefix(state)
        reason = task.get("plan_reason") or "现在出发比较顺路"
        ev = task.get("evidence") or {}
        line = f"{prefix}该去取快递了：{task.get('title', '')}"
        if ev.get("pickup"):
            line += f"，取件码 {ev['pickup']}"
        line += f"。{reason}。取完回我「已取」，我就归档。"
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
                    by: str = "owner") -> Optional[str]:
        text = str(text or "").strip()
        if not text:
            return None
        now = float(now or time.time())
        task = self.store.latest_active()
        if task is None:
            # 没有活动任务时不抢普通聊天：带“快递/待办/任务”字样的闲聊
            # 交给正常对话流程，绝不再回“现在没有待确认的待办任务。”
            # （之前这句内部腔调会绕过 LLM 直接漏进聊天，看起来像日志泄露）
            return None
        if not (CONFIRM_RX.search(text) or IGNORE_RX.search(text)
                or DONE_RX.search(text) or DEFER_RX.search(text)
                or TASK_WORD_RX.search(text)):
            return None
        if DONE_RX.search(text):
            return self.mark_done(task, now, by)
        if IGNORE_RX.search(text):
            return self.mark_ignored(task, now, by)
        if DEFER_RX.search(text):
            return self.defer(task, now, by)
        explicit_confirm = bool(CONFIRM_RX.search(text))
        task_action = bool(TASK_WORD_RX.search(text)) and any(
            w in text for w in ("取", "拿", "安排", "排", "帮"))
        if explicit_confirm or task_action:
            if task.get("status") in ("pending_confirm", "awaiting_confirm", "deferred"):
                return self.plan(task, now, by)
            if task.get("status") in ("planned", "reminded"):
                return f"已经在计划里了：{task.get('title', '')}。到点我会提醒你。"
        return None

    def find_by_text(self, text: str) -> Optional[Dict[str, Any]]:
        text = str(text or "")
        for task in self.store.tasks:
            if task.get("id") in text:
                return task
        return self.store.latest_active()

    def tool_confirm(self, task_id: str = "", note: str = "") -> str:
        task = self.store.find(task_id) if task_id else self.store.latest_active()
        if not task:
            return "没有这个任务。用 task_list 查看当前待办。"
        if task.get("status") in ("pending_confirm", "awaiting_confirm", "deferred"):
            return self.plan(task, by="tool")
        return f"{task.get('title', '')} 当前是 {STATUS_LABELS.get(task.get('status'), task.get('status'))}，不需要再确认。"

    def tool_done(self, task_id: str = "") -> str:
        task = self.store.find(task_id) if task_id else self.store.latest_active()
        if not task:
            return "没有这个任务。"
        return self.mark_done(task, by="tool")

    def tool_ignore(self, task_id: str = "") -> str:
        task = self.store.find(task_id) if task_id else self.store.latest_active()
        if not task:
            return "没有这个任务。"
        return self.mark_ignored(task, by="tool")
