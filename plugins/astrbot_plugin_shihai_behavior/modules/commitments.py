"""承诺账簿：把"答应了要做的事"变成可核验的对象，而不是一句话的记忆。

为什么需要它（论文 7.2 的问题陈述）：
记忆库能召回"我说过要帮你查"，但召回是**语义相似度**驱动的——它只在你提起
相关话题时出现。生活助理的失败恰恰发生在"人没提"的时候：答应周三交的东西，
周三早上不会因为没人提起就自动冒出来。所以承诺必须与记忆分开存，
按**时间**而不是按**相似度**被看见。

数据结构刻意最小：一条承诺 = (谁答应的, 答应什么, 什么时候到期, 状态)。
不做自然语言解析——到期时间由调用方（工具/抽取）给定，解析错了比没有更糟。

存储：commitments.json（与 fact.json 同目录），list 结构，逐条 status 流转：
    open → done / missed
读接口一律按会话 tag 过滤，跨会话不串（同一个人在群里答应的事，私聊里也能
看到——这是有意设计：承诺属于"这个人"，不属于"这个场域"；但别人的承诺
绝不出现，也不脱敏泄露）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_MISSED = "missed"

DUE_SOON_SEC = 2 * 3600      # 2 小时内到期 → 提示"快到期"
OVERDUE_GRACE = 30 * 60      # 过期 30 分钟后才算 missed，给时差错峰留余量
# 提醒只发主人：发给承诺对象既是骚扰（对方不认识这个 bot），还可能把主人的私事
# 写进另一个人手里。任何情况下承诺提醒都不对外。
REMINDER_OWNER_ONLY = True

# 抽取器（v2，按"宁漏不脏"重做）：
# 相对时间是开放集——"大后天下午那个会后""下周找个时间""月底前""等我忙完这阵"
# 没有一条能被正则可靠解析。放宽 → 误记（半夜发提醒）；收紧 → 漏记（账簿是空的）。
# 所以只认**绝对时间白名单**，白名单之外一律不抽。宁可漏，不可错。
_ABSOLUTE_RX = (
    re.compile(r"明天.{0,4}\d{1,2}\s*[点时:]"),        # 明天8点 / 明天上午10点
    re.compile(r"后天.{0,4}\d{1,2}\s*[点时:]"),        # 后天3点
    re.compile(r"周[一二三四五六日天].{0,4}\d{0,2}\s*[点时:]?"),  # 周三 / 周三3点
    re.compile(r"\d{1,2}月\d{1,2}[日号].{0,4}\d{0,2}\s*[点时:]?"),  # 11月15日 / 11月15日9点
)
_CUE_RX = re.compile(r"(提醒我|记得提醒|别忘了|帮我|答应|约好|回头|待会儿|晚点|明天要|后天要)")
# 正文里的时间/提示语：模型立账时往往把时间抄进 text，提醒时会念两遍。
_TIME_STRIP_RX = re.compile(
    r"(?:大后天|后天|明天|今天|明早|今晚)"
    r"|(?:\d{1,2}月\d{1,2}[日号]?)"
    r"|(?:周[一二三四五六日天]|星期[一二三四五六日天])"
    r"|(?:早上|上午|早晨|中午|下午|傍晚|晚上|凌晨|夜里)"
    r"|(?:\d{1,2}\s*[点:：]\s*\d{0,2}\s*(?:分|半)?)"
    r"|(?:提醒我|提醒一下我|叫我一下|叫一下|叫我|帮我|到时候|到点|记得|别忘了)"
)
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_due_ts(text: str, now: Optional[float] = None) -> float:
    """把一句中文里的时间词折成时间戳；认不出就返回 0（不设期限）。"""
    now_dt = datetime.fromtimestamp(float(now or time.time()))
    t = str(text or "")
    hour = 20
    m = re.search(r"\d{1,2}\s*[点时]", t)
    if m:
        try:
            hour = int(re.findall(r"\d{1,2}", m.group(0))[0])
        except (IndexError, ValueError):
            hour = 20
    base = now_dt
    if "大后天" in t:
        base = now_dt + timedelta(days=3)
    elif "后天" in t:
        base = now_dt + timedelta(days=2)
    elif "明早" in t:
        base, hour = now_dt + timedelta(days=1), 9
    elif "明晚" in t:
        base, hour = now_dt + timedelta(days=1), 21
    elif "明天" in t:
        base = now_dt + timedelta(days=1)
    elif "今晚" in t:
        hour = 21
    elif "今天" in t:
        pass
    elif re.search(r"周[一二三四五六日天]", t):
        wd_ch = re.findall(r"周([一二三四五六日天])", t)[-1]
        target = _WEEKDAY[wd_ch]
        delta = (target - now_dt.weekday()) % 7
        base = now_dt + timedelta(days=delta or 7)
    elif re.search(r"\d{1,2}月\d{1,2}[日号]", t):
        mm = re.findall(r"(\d{1,2})月(\d{1,2})", t)[-1]
        try:
            base = now_dt.replace(month=int(mm[0]), day=int(mm[1]))
            if base < now_dt:
                base = base.replace(year=base.year + 1)
        except ValueError:
            return 0.0
    elif "待会" in t or "晚点" in t or "回头" in t:
        return float(now or time.time()) + 3 * 3600
    else:
        return 0.0
    base = base.replace(hour=min(max(hour, 0), 23), minute=0, second=0, microsecond=0)
    ts = base.timestamp()
    if ts < float(now or time.time()) - 3600:   # 时间词指向过去，不立期限
        return 0.0
    return ts


def is_absolute(text: str) -> bool:
    """白名单判定：命中任一绝对时间模式才算"有明确时间点"。"""
    t = str(text or "")
    return any(rx.search(t) for rx in _ABSOLUTE_RX)


def extract(text: str, now: Optional[float] = None) -> Optional[tuple[str, float, str]]:
    """抽一条承诺 → (内容原文, 到期时间戳, 时间原文)。

    - 白名单外一律不抽（宁漏不脏）；
    - 提问句一律不抽（"要不要提醒我"不是答应）；
    - 时间原文一并返回：**显示用原文、调度用时间戳**，两者分离。这样即使
      解析错了，她读到的仍是"明天8点"，不会出现"显示和解析打架"。
    """
    t = str(text or "").strip()
    if len(t) < 6 or len(t) > 200:
        return None
    if not _CUE_RX.search(t) or not is_absolute(t):
        return None
    if re.search(r"(要不要|是不是|还用不用|吗[？?])", t):
        return None                       # 提问不是答应
    hit = ""
    for rx in _ABSOLUTE_RX:
        m = rx.search(t)
        if m:
            hit = m.group(0)
            break
    return t[:120], parse_due_ts(t, now), hit


@dataclass
class Commitment:
    cid: str
    tag: str                 # 承诺归属：owner / qq:<号> / group:<群号>
    text: str                # 答应的事，一句话（原文）
    due_ts: float = 0.0      # 0 = 没有明确时间（"改天"），只做背景不催
    due_text: str = ""       # 时间原文（"明天8点"）：显示用原文、调度用时间戳
    kind: str = "plan"       # plan / promise / reminder
    status: str = STATUS_OPEN
    created_ts: float = field(default_factory=time.time)
    src: str = ""            # 来源会话（unified_msg_origin），便于追责与实验统计
    reminded_ts: float = 0.0 # 首次主动提醒的时刻；0 = 未提醒（去重键，不改 status）


class CommitmentLedger:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self.items: Dict[str, Commitment] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw if isinstance(raw, list) else raw.get("items", [])
            for r in rows:
                if not isinstance(r, dict) or not r.get("cid"):
                    continue
                try:
                    # 只传存在且非 None 的键：老数据没有 reminded_ts 时会显式传
                    # None，把 dataclass 的默认值 0.0 覆盖成 None，后面做算术就炸
                    kw = {k: r.get(k) for k in
                          ("cid", "tag", "text", "due_ts", "due_text",
                           "kind", "status", "created_ts", "src",
                           "reminded_ts")}
                    kw = {k: v for k, v in kw.items() if v is not None}
                    it = Commitment(**kw)
                except TypeError:
                    continue
                self.items[it.cid] = it
        except Exception:  # noqa: BLE001 - 坏了就当空账
            self.items = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([asdict(x) for x in self.items.values()],
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---------- 写 ----------
    def add(self, tag: str, text: str, due_ts: float = 0.0,
            kind: str = "plan", src: str = "", due_text: str = "",
            save: bool = True) -> str:
        t = str(text or "").strip()
        if not t:
            return ""
        seed = f"{tag}|{t}|{float(due_ts or 0):.0f}"
        cid = "cm:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        if cid in self.items:
            self.items[cid].status = STATUS_OPEN
            self.items[cid].due_ts = float(due_ts or self.items[cid].due_ts)
            # 同一件事被重新答应一次 → 算新的一次，允许再提醒
            self.items[cid].reminded_ts = 0.0
            if due_text:
                self.items[cid].due_text = str(due_text)[:40]
        else:
            self.items[cid] = Commitment(
                cid=cid, tag=str(tag or "owner"), text=t[:120],
                due_ts=float(due_ts or 0.0), due_text=str(due_text or "")[:40],
                kind=str(kind or "plan"), src=str(src or ""))
        if save:
            self.save()
        return cid

    def mark(self, cid: str, status: str, save: bool = True) -> bool:
        it = self.items.get(str(cid))
        if not it:
            return False
        it.status = status
        if save:
            self.save()
        return True

    def mark_done(self, cid: str) -> bool:
        return self.mark(cid, STATUS_DONE)

    # ---------- 读 ----------
    def open_items(self, tag: str, now: Optional[float] = None) -> List[Commitment]:
        now = float(now or time.time())
        rows = [x for x in self.items.values()
                if x.status == STATUS_OPEN and self._visible(x.tag, str(tag or "owner"))]
        return sorted(rows, key=lambda x: (x.due_ts or now + 1e9, x.created_ts))

    @staticmethod
    def _visible(item_tag: str, viewer: str) -> bool:
        """承诺按**归属人**隔离，不外泄：owner 层只给 owner 自己看。

        与记忆层的 w 门控不同——记忆可以按亲密度分享片段，承诺不行：
        "我答应过鑫鑫什么"一旦出现在别人的会话里，既尴尬又解释了不该解释的事。
        所以这里不分档，只有自己的看得见。"""
        it, vw = str(item_tag or "owner"), str(viewer or "owner")
        return it == vw

    def due_soon(self, tag: str, within: float = DUE_SOON_SEC,
                 now: Optional[float] = None) -> List[Commitment]:
        now = float(now or time.time())
        return [x for x in self.open_items(tag, now)
                if x.due_ts and now <= x.due_ts <= now + within]

    def overdue(self, tag: str, now: Optional[float] = None) -> List[Commitment]:
        now = float(now or time.time())
        return [x for x in self.open_items(tag, now)
                if x.due_ts and x.due_ts < now - OVERDUE_GRACE]

    def sweep(self, now: Optional[float] = None) -> int:
        """看门狗调用：把过期很久的 open 标成 missed，避免账簿无限膨胀。"""
        now = float(now or time.time())
        n = 0
        for x in self.items.values():
            if x.status == STATUS_OPEN and x.due_ts and x.due_ts < now - 24 * 3600:
                x.status = STATUS_MISSED
                n += 1
        if n:
            self.save()
        return n

    def brief(self, tag: str, now: Optional[float] = None, max_n: int = 3) -> str:
        """给注入用的一句话清单：只列最紧的几条，过期的不催不藏。"""
        now = float(now or time.time())
        rows = self.open_items(tag, now)
        if not rows:
            return ""
        lines = []
        for x in rows[: max(1, int(max_n))]:
            if x.due_ts:
                left = x.due_ts - now
                if left < 0:
                    when = "已经过了"
                elif left < 3600:
                    when = "还有 %d 分钟" % max(1, int(left // 60))
                elif left < 48 * 3600:
                    when = "还有 %d 小时" % int(left // 3600)
                else:
                    when = "还有 %d 天" % int(left // 86400)
                spec = f"{x.due_text}，" if x.due_text else ""
                lines.append(f"- {x.text}（{spec}{when}）")
            else:
                lines.append(f"- {x.text}（没说时间）")
        return "【你答应过还没做完的事】\n" + "\n".join(lines)

    # ---------- 工具入口（主人明确要求时记账，比自动抽取可控） ----------
    def remember(self, tag: str, text: str, when: str = "",
                 src: str = "") -> tuple[bool, str]:
        """立一笔账：`remember_commitment` 工具的落点。

        与自动抽取（extract）分工：抽取是"宁漏不脏"的窄口，几乎不命中；这里是
        主人明确说"帮我记着"时的宽口，但仍**只认绝对时间白名单**——因为提醒
        的准点能力来自时间戳，判不准就不该答应"到点叫你"。
        返回 (是否成功, 一句话说明)，由工具原样回给模型。
        """
        body = str(text or "").strip()
        if not body:
            return False, "要记的事还是空的，你先把事情说出来我再记。"
        if str(tag or "owner") != "owner":
            # 这本账只装主人自己的事：别人的托付记进来，日后必被"要不要替他
            # 联系一下"诱惑，而这正是 REMINDER_OWNER_ONLY 要挡的东西。
            return False, "这本账只记你自己答应的事，别人托的事我不往里记。"
        when_text = str(when or "").strip() or body
        if not is_absolute(when_text):
            return False, ("时间我拿不准——说成「明天8点」「周三下午三点」"
                           "「11月15日9点」这种，我才能准点到。")
        due_ts = parse_due_ts(when_text, time.time())
        if not due_ts:
            return False, "这个时间我算不出来，换个说法吧（比如「明天8点」）。"
        # 时间原文：优先用户给的 when；没给就从整句里只截出时间片段
        # （否则 due_text 会是整句，提醒时变成"明天8点帮我叫一下我起床 ——
        #  明天8点帮我叫一下我起床"，读两遍）
        due_text = str(when or "").strip() or self.pick_due_text(when_text)
        # 模型常把时间又抄进正文（"早上7:30提醒我起床"），提醒时会念两遍时间
        # （"提醒你：明天早上7:30 —— 早上7:30提醒我起床"）。剔掉时间/提示语，
        # 只要还剩两个字以上就用剔过的版本；剔空了（整句只有时间）就保留原文。
        trimmed = self.strip_time(body)
        if len(trimmed) >= 2:
            body = trimmed
        self.add(tag, body, due_ts, kind="promise", src=str(src or ""),
                 due_text=(due_text or when_text)[:40])
        return True, (f"记下了：{due_text or when_text}，{body}。"
                      f"到点我自己叫你，不用你再记着。")

    @staticmethod
    def strip_time(text: str) -> str:
        """剔掉一句话里的时间表达与提示语，只留事情本身。"""
        s = _TIME_STRIP_RX.sub(" ", str(text or ""))
        s = re.sub(r"[\s，,。、；;：:！!？?~～]+", " ", s).strip()
        return s.strip(" ，,。、")

    @staticmethod
    def pick_due_text(text: str) -> str:
        """从一句话里只截出时间片段（"明天8点"），给 due_text 用。"""
        for rx in _ABSOLUTE_RX:
            m = rx.search(str(text or ""))
            if m:
                return m.group(0).strip()
        return ""

    # ---------- 主动出口（论文 6.18.3：承诺必须能"到点自己冒出来"） ----------
    def pending_reminders(self, now: Optional[float] = None,
                          lead: float = DUE_SOON_SEC) -> List[Commitment]:
        """该现在提醒的条目：未提醒过、有明确时间、已进入 lead 窗口（含已过期）。

        与 due_soon 的区别是**多一个 reminded_ts 去重键**：due_soon 只问"快到期了
        吗"，它每轮都会返回同一条；这里只返回"还没提醒过的那一次"。
        """
        now = float(now or time.time())
        rows = [x for x in self.items.values()
                if x.status == STATUS_OPEN and x.due_ts
                and not x.reminded_ts
                and now >= x.due_ts - float(lead)]
        return sorted(rows, key=lambda x: x.due_ts)

    def mark_reminded(self, cid: str, ts: Optional[float] = None,
                      save: bool = True) -> bool:
        it = self.items.get(str(cid))
        if not it:
            return False
        it.reminded_ts = float(ts or time.time())
        if save:
            self.save()
        return True

    @staticmethod
    def format_reminder(item: Commitment) -> str:
        """文案：`提醒你：{due_text} —— {content}`。

        due_text 必须是用户原话（"明天8点"），**不重新解析、不翻译**：解析错了，
        她念出来的仍是原话，不会出现"你说的和我算的对不上"。
        """
        due = str(getattr(item, "due_text", "") or "").strip() or "到点了"
        return f"提醒你：{due} —— {item.text}"

    @staticmethod
    def guard_owner_target(target: str, owner_qid: str) -> None:
        """投递前最后一道闸：目标必须是主人私聊，否则拒绝发送。

        承诺是主人自己的账，绝不能替主人去联系任何人（REMINDER_OWNER_ONLY）。
        """
        if not REMINDER_OWNER_ONLY:
            return
        if str(owner_qid or "") not in str(target or ""):
            raise PermissionError(
                f"承诺提醒只能投递给主人（target={target!r}），已拒绝")
