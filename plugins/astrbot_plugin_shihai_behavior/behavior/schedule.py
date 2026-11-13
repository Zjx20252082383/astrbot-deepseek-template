"""课表：周次/单双周计算、被动查询、课前提醒判定与去重。"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from astrbot.api import logger


class ScheduleStore:
    def __init__(self, json_path: Path, fired_path: Path,
                 watcher_interval: int):
        self.json_path = Path(json_path)
        self.fired_path = Path(fired_path)
        self.watcher_interval = int(watcher_interval)
        self._data: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        with open(self.json_path, encoding="utf-8") as fh:
            self._data = json.load(fh)
        logger.info(
            "[课表] 已加载 %d 条排课、%d 个节次",
            len(self._data.get("courses", [])),
            len(self._data.get("sections", {})))

    def current_week(self, now: Optional[float] = None) -> int:
        dt = datetime.fromtimestamp(now or time.time())
        monday = dt.date() - timedelta(days=dt.weekday())
        w1 = date.fromisoformat(self._data["meta"]["week1_monday"])
        return (monday - w1).days // 7 + 1

    @staticmethod
    def _course_active_in(course: Dict[str, Any], week: int) -> bool:
        lo, hi = course["week_range"]
        if not (lo <= week <= hi):
            return False
        parity = course.get("parity", "all")
        if parity == "even":
            return week % 2 == 0
        if parity == "odd":
            return week % 2 == 1
        return True

    def courses_of(self, week: int, weekday: int) -> List[Dict[str, Any]]:
        out = [c for c in self._data["courses"]
               if int(c["weekday"]) == weekday
               and self._course_active_in(c, week)]
        return sorted(out, key=lambda c: int(c["start"]))

    def _start_ts(self, now: float, course: Dict[str, Any]) -> float:
        hm = self._data["sections"][str(course["start"])]
        base = datetime.fromtimestamp(now)
        hh, mm = (int(x) for x in hm.split(":"))
        return base.replace(hour=hh, minute=mm, second=0,
                            microsecond=0).timestamp()

    @staticmethod
    def _display_name(course: Dict[str, Any]) -> str:
        return course.get("short") or course["name"]

    def format_day(self, week: int, weekday: int, title: str = "") -> str:
        courses = self.courses_of(week, weekday)
        if not courses:
            return (title + "这天没课。") if title else "这天没课。"
        lines = [title] if title else []
        for c in courses:
            hm = self._data["sections"][str(c["start"])]
            end_hm = self._data["sections"][str(c["end"])]
            lines.append(
                f"第{c['start']}-{c['end']}节 {hm}-{end_hm} "
                f"{self._display_name(c)}（{c['location']}）")
        return "\n".join(lines)

    def due_reminders(self, now: float, lead_minutes: int,
                      is_quiet: bool) -> List[tuple]:
        """返回此刻到提醒窗口、且当天未去重的 (day_key, key, message)。

        只判定、不写 fired：调用方发送成功后再调 mark_fired，
        这样发送失败时下一轮仍会重试，不会因提前标记而丢提醒。
        """
        if is_quiet:
            return []
        dt = datetime.fromtimestamp(now)
        week = self.current_week(now)
        weekday = dt.weekday() + 1
        day_key = dt.strftime("%Y-%m-%d")
        fired = self._load_fired().get(day_key, [])
        result: List[tuple] = []
        for c in self.courses_of(week, weekday):
            start_ts = self._start_ts(now, c)
            remind_ts = start_ts - lead_minutes * 60
            if remind_ts <= now < start_ts:
                key = f"{c['short']}|{c['start']}-{c['end']}"
                if key in fired:
                    continue
                hm = self._data["sections"][str(c["start"])]
                msg = (f"再过{lead_minutes}分钟有{self._display_name(c)}"
                       f"（{hm}，{c['location']}），别迟到。")
                result.append((day_key, key, msg))
        return result

    def _load_fired(self) -> Dict[str, List[str]]:
        try:
            with open(self.fired_path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except (OSError, ValueError):
            return {}

    def mark_fired(self, day_key: str, key: str) -> None:
        data = self._load_fired()
        data = {day_key: sorted(set(data.get(day_key, []) + [key]))}
        self.fired_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.fired_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        tmp.replace(self.fired_path)
