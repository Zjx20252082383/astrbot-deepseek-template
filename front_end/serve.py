# -*- coding: utf-8 -*-
"""小鱼心智控制台的本地后端。

做两件事：
1. 把观测台 HTML 从 http://127.0.0.1:6191/ 提供出去（避免 file:// 打不开 fetch）；
2. 作为 AstrBot 的反向代理：登录拿 dashboard JWT，再把 /api/plug/astrbot_plugin_shihai/{state,cmd}
   转发给真正在跑的心智核。前端只跟本进程说话，数据永远来自同一个 engine 实例。

用法：python serve.py   （然后浏览器打开 http://127.0.0.1:6191/）
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
# AstrBot 运行数据目录：默认取本目录的上一级（front_end 与 data/ 并列时即运行根），
# 若部署位置不同，用环境变量 SHIHAI_DATA_ROOT 指向含 data/ 的目录。
DATA_ROOT = Path(os.environ.get("SHIHAI_DATA_ROOT", str(ROOT)))
HTML_FILE = BASE / "识海心智观测台.html"
CONFIG_FILE = DATA_ROOT / "data" / "cmd_config.json"
LOG_FILE = DATA_ROOT / "data" / "logs" / "astrbot.log"
# 核心每 tick 消费的摄入通道（读取后删除）；短信等外部事件经此写入记忆。
ACTION_LOG_FILE = DATA_ROOT / "data" / "plugin_data" / "shihai" / "action_log.jsonl"
_ingest_lock = threading.Lock()

HOST = "127.0.0.1"
PORT = 6191
ASTRBOT = os.environ.get("ASTRBOT_BASE", "http://127.0.0.1:6185")
PLUGIN = "astrbot_plugin_shihai"

# 可选的局域网/令牌配置：存在 serve_config.json 或用环境变量时才放开；
# 直接双击运行仍默认只监听本机、无鉴权。
SERVE_CONFIG = BASE / "serve_config.json"


def _load_serve_config() -> dict:
    try:
        with open(SERVE_CONFIG, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


_scfg = _load_serve_config()
HOST = os.environ.get("SHIHAI_HOST", str(_scfg.get("host", "127.0.0.1")))
ACCESS_TOKEN = os.environ.get("SHIHAI_TOKEN", str(_scfg.get("token", "")))

_LOG_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<src>[^\]]+)\]\s+\[(?P<lvl>[A-Z]+)\]\s+\[(?P<loc>[^\]]+)\]:\s*(?P<msg>.*)$")
_LEVEL_CN = {"INFO": "INFO", "WARN": "WARNING", "WARNING": "WARNING", "ERROR": "ERROR",
             "DEBUG": "DEBUG", "CRIT": "ERROR", "CRITICAL": "ERROR"}


def _append_action_log(text: str, now: float | None = None) -> None:
    """把一条外部事件按核心摄入格式追加到 action_log.jsonl（每行一个 JSON）。"""
    text = (text or "").strip()
    if not text:
        return
    entry = {"text": text, "ts": float(now or time.time())}
    with _ingest_lock:
        ACTION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTION_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- 短信非 LLM 规则过滤（写 action_log 之前拦截，无外部依赖） ----------
FILTER_STATS_FILE = BASE / "sms_filter_stats.json"
_URL_RE = re.compile(r"https?://[^\s]+|www\.[^\s]+", re.IGNORECASE)
_DEFAULT_VERIFY_KW = ["验证码", "校验码", "动态码", "安全码", "验证代码", "短信码", "口令", "取件码"]
_DEFAULT_MARKETING_KW = ["退订", "回T", "回TD", "回N", "回t", "拒收", "免打扰", "拒绝接收"]


class SmsFilter:
    """纯规则、无 LLM、无外部依赖的短信前置过滤。线程安全，计数持久化。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.lock = threading.Lock()
        vcfg = self.cfg.get("verification", {})
        lo = max(1, int(vcfg.get("min_digits", 4)))
        hi = max(lo, int(vcfg.get("max_digits", 8)))
        self._digit_re = re.compile(r"(?<!\d)\d{" + str(lo) + r"," + str(hi) + r"}(?!\d)")
        self._hits: dict[str, list[float]] = {}
        self.stats = self._load_stats()

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def _load_stats(self) -> dict:
        base = {"total": 0, "passed": 0, "dropped": 0, "truncated": 0,
                "by_reason": {}, "recent_blocked": []}
        try:
            if FILTER_STATS_FILE.exists():
                with open(FILTER_STATS_FILE, "r", encoding="utf-8") as fh:
                    saved = json.load(fh)
                for k in base:
                    if k in saved:
                        base[k] = saved[k]
        except Exception:  # noqa: BLE001
            pass
        return base

    def _save_stats(self) -> None:
        try:
            with open(FILTER_STATS_FILE, "w", encoding="utf-8") as fh:
                json.dump(self.stats, fh, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    def _drop(self, reason: str, sender: str, content: str) -> tuple:
        self.stats["dropped"] += 1
        self.stats["by_reason"][reason] = self.stats["by_reason"].get(reason, 0) + 1
        self.stats["recent_blocked"].append({
            "ts": int(time.time()), "sender": sender, "reason": reason,
            "content": (content or "")[:80]})
        self.stats["recent_blocked"] = self.stats["recent_blocked"][-20:]
        self._save_stats()
        return False, reason, content

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.stats, ensure_ascii=False))

    def check(self, sender: str, content: str) -> tuple:
        """返回 (是否放行, 原因, 可能截断后的正文)。调用方已确认 enabled。"""
        now = time.time()
        with self.lock:
            sender = (sender or "").strip()
            text = (content or "").strip()
            self.stats["total"] += 1

            cap = int(self.cfg.get("max_length", 200) or 0)
            if cap and len(text) > cap:
                text = text[:cap]
                self.stats["truncated"] += 1

            block = [str(x).strip() for x in self.cfg.get("sender_blocklist", [])]
            if sender and sender in block:
                return self._drop("blocklist", sender, content)

            allow = [str(x).strip() for x in self.cfg.get("sender_allowlist", [])]
            if allow and sender not in allow:
                return self._drop("not_allowlisted", sender, content)

            rl = self.cfg.get("rate_limit", {})
            if rl.get("enabled", True):
                limit = int(rl.get("per_minute", 5))
                arr = [t for t in self._hits.get(sender, []) if now - t < 60]
                if len(arr) >= limit:
                    self._hits[sender] = arr
                    return self._drop("rate_limited", sender, content)
                arr.append(now)
                self._hits[sender] = arr

            v = self.cfg.get("verification", {})
            if v.get("enabled", True):
                kws = v.get("keywords", _DEFAULT_VERIFY_KW)
                if any(k in content for k in kws) and self._digit_re.search(content):
                    return self._drop("verification", sender, content)

            mk = self.cfg.get("marketing", {})
            if mk.get("enabled", True):
                kws = mk.get("keywords", _DEFAULT_MARKETING_KW)
                prefixes = [str(x).strip() for x in mk.get("sender_prefixes", [])]
                if any(k in content for k in kws) or \
                        (sender and any(sender.startswith(p) for p in prefixes)):
                    return self._drop("marketing", sender, content)

            pl = self.cfg.get("pure_link", {})
            if pl.get("enabled", True) and not _URL_RE.sub("", content).strip():
                return self._drop("pure_link", sender, content)

            self.stats["passed"] += 1
            self._save_stats()
            return True, "ok", text


SMS_FILTER = SmsFilter(_scfg.get("sms_filter", {}))


def _read_tail(path: Path, limit: int = 400) -> list[str]:
    """按字节倒着读，避免把 19MB 的全量日志读进内存。"""
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = min(size, 512 * 1024)
        fh.seek(size - block)
        raw = fh.read().decode("utf-8", "replace")
    return raw.splitlines()[-limit:]


_WORD_MAP = (("dafeiyu_core", "shihai_core"), ("dafeiyu", "shihai"), ("小鱼", "数字生命体"), ("小助手", "数字生命体"))


def _sanitize(text):
    """日志常带插件内部旧模块名，展示前统一改写。"""
    if not text:
        return text
    for a, b in _WORD_MAP:
        text = text.replace(a, b)
    return text


def _log_rows(limit: int = 120):
    rows = []
    for line in _read_tail(LOG_FILE, limit * 2):
        line = line.rstrip()
        if not line:
            continue
        m = _LOG_RE.match(line)
        if m:
            rows.append({
                "ts": m.group("ts")[11:19] if len(m.group("ts")) > 11 else m.group("ts"),
                "level": _LEVEL_CN.get(m.group("lvl").upper(), m.group("lvl").upper()),
                "src": m.group("loc"),
                "text": _sanitize(m.group("msg").strip()),
            })
        elif rows:
            rows[-1]["text"] += " " + line.strip()
    for r in rows:
        r["text"] = _sanitize(r["text"])
    return rows[-limit:]


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _established_to(port: int) -> int:
    """数一下有多少条已建立的连接到该端口：反向 ws 里就是 QQ 侧真的连上来了。"""
    try:
        import psutil  # 可选依赖
    except Exception:  # noqa: BLE001
        return -1
    n = 0
    for conn in psutil.net_connections(kind="inet"):
        if conn.status != "ESTABLISHED":
            continue
        try:
            if conn.laddr and conn.laddr.port == port:
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def _platform_ports():
    ports = []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        for item in cfg.get("platform") or []:
            p = item.get("ws_reverse_port") or item.get("port")
            if p:
                ports.append(int(p))
    except Exception:  # noqa: BLE001
        pass
    return ports or [6199]


def _link_state():
    astrbot_up = False
    try:
        req = urllib.request.Request(ASTRBOT + "/api/stat/start-time")
        with urllib.request.urlopen(req, timeout=3) as resp:
            astrbot_up = resp.status == 200
    except Exception:  # noqa: BLE001
        astrbot_up = _port_open("127.0.0.1", int(ASTRBOT.rsplit(":", 1)[-1]))
    ports = _platform_ports()
    qq = False
    detail = []
    for p in ports:
        est = _established_to(p)
        if est > 0:
            qq = True
        detail.append({"port": p, "established": est, "listening": _port_open("127.0.0.1", p)})
    return {"astrbot": astrbot_up, "qq": qq, "platforms": detail, "at": int(time.time())}


_lock = threading.Lock()
_token = {"value": "", "at": 0.0}


def _dashboard_auth() -> tuple[str, str]:
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    dash = cfg.get("dashboard") or {}
    return str(dash.get("username", "astrbot")), str(dash.get("password", ""))


def _http_json(method: str, url: str, body=None, token: str = "", timeout=8):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8-sig"))


def ensure_token(force: bool = False) -> str:
    with _lock:
        if not force and _token["value"] and time.time() - _token["at"] < 1800:
            return _token["value"]
        user, pwd = _dashboard_auth()
        status, payload = _http_json(
            "POST", ASTRBOT + "/api/auth/login", {"username": user, "password": pwd})
        if payload.get("status") != "ok":
            raise RuntimeError("dashboard 登录失败")
        _token["value"] = payload["data"]["token"]
        _token["at"] = time.time()
        return _token["value"]


def call_plugin(method: str, tail: str, body=None):
    """tail = 'state' | 'cmd'。401 时刷新 token 重试一次。"""
    url = f"{ASTRBOT}/api/plug/{PLUGIN}/{tail}"
    for attempt in (0, 1):
        token = ensure_token(force=attempt == 1)
        try:
            return _http_json(method, url, body, token)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raw = e.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {e.code}: {raw}")
    raise RuntimeError("鉴权失败")


def call_dashboard(method: str, path: str, body=None, query: str = ""):
    """转发到 dashboard 原生 /api 路径（cron/persona），401 自动重登一次。"""
    url = ASTRBOT + path + (("?" + query) if query else "")
    for attempt in (0, 1):
        token = ensure_token(force=attempt == 1)
        try:
            return _http_json(method, url, body, token)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raw = e.read().decode("utf-8", "replace")[:500]
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"status": "error", "message": raw}
    raise RuntimeError("鉴权失败")


def _default_session() -> str:
    """根据当前平台 id 与用户 QQ 拼出 cron 目标会话。"""
    platform_id = "qq"
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
        plats = cfg.get("platform") or []
        if plats:
            platform_id = str(plats[0].get("id") or platform_id)
    except Exception:  # noqa: BLE001
        pass
    return f"{platform_id}:FriendMessage:1000000001"


GRAPH_FILE = ROOT / "data" / "plugin_data" / "shihai" / "shihai_graph.json"
TASKS_FILE = ROOT / "data" / "plugin_data" / "shihai" / "tasks.json"
TASK_CONTEXT_FILE = ROOT / "data" / "plugin_data" / "shihai" / "task_context.json"


def _read_json_file(path: Path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


DEFAULT_TASK_CONTEXT = {
    "timezone": "Asia/Shanghai",
    "class_end": "16:00",
    "station_close": "18:00",
    "walk_minutes": 8,
    "pickup_location": "菜鸟驿站",
    "quiet_hours": [23, 7],
    "remind_before_close_minutes": 90,
}
_HHMM_RX = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _write_json_atomic(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _as_hhmm(value: str) -> str:
    m = _HHMM_RX.match(str(value).strip())
    if not m:
        return ""
    return "%02d:%s" % (int(m.group(1)), m.group(2))


def _update_task_context(patch: dict):
    """校验并合并写回 task_context.json，返回 (ok, message, merged)。"""
    current = {}
    try:
        current = _read_json_file(TASK_CONTEXT_FILE)
    except (OSError, ValueError):
        current = {}
    merged = dict(DEFAULT_TASK_CONTEXT)
    merged.update({k: v for k, v in current.items() if k in DEFAULT_TASK_CONTEXT})
    errors = []

    if "class_end" in patch:
        v = _as_hhmm(patch["class_end"])
        if v:
            merged["class_end"] = v
        else:
            errors.append("下课时间需为 HH:MM")
    if "station_close" in patch:
        v = _as_hhmm(patch["station_close"])
        if v:
            merged["station_close"] = v
        else:
            errors.append("驿站关门时间需为 HH:MM")
    if "walk_minutes" in patch:
        try:
            iv = int(str(patch["walk_minutes"]).strip())
            if iv >= 1:
                merged["walk_minutes"] = iv
            else:
                errors.append("步行分钟需为正整数")
        except ValueError:
            errors.append("步行分钟需为整数")
    if "pickup_location" in patch:
        v = str(patch["pickup_location"]).strip()
        if v:
            merged["pickup_location"] = v
        else:
            errors.append("取件地点不能为空")
    if "remind_before_close_minutes" in patch:
        try:
            iv = int(str(patch["remind_before_close_minutes"]).strip())
            if iv >= 1:
                merged["remind_before_close_minutes"] = iv
            else:
                errors.append("提前提醒分钟需为正整数")
        except ValueError:
            errors.append("提前提醒分钟需为整数")
    if "quiet_hours" in patch:
        try:
            qh = patch["quiet_hours"]
            if isinstance(qh, list) and len(qh) == 2:
                a, b = int(qh[0]), int(qh[1])
                if 0 <= a <= 23 and 0 <= b <= 23:
                    merged["quiet_hours"] = [a, b]
                else:
                    errors.append("静默时段小时需在 0-23")
            else:
                errors.append("静默时段需为两个小时值")
        except (ValueError, TypeError):
            errors.append("静默时段格式有误")

    if errors:
        return False, "；".join(errors), merged
    merged["source"] = "user_configured"
    _write_json_atomic(TASK_CONTEXT_FILE, merged)
    return True, "ok", merged


def _memory_items(q: str = "", limit: int = 30, offset: int = 0, sort: str = "created"):
    """直接读图谱文件，返回记忆节点（只读，不经过会自行覆写的 engine）。"""
    with open(GRAPH_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    nodes = data.get("nodes", {})
    q = (q or "").strip().lower()
    items = []
    for nid, nd in nodes.items():
        content = str(nd.get("content", ""))
        if q and q not in content.lower():
            continue
        items.append({
            "id": nid,
            "type": str(nd.get("type", "") or ""),
            "sender": str(nd.get("sender", "") or ""),
            "content": content,
            "created": int(nd.get("created", 0) or 0),
            "a": round(float(nd.get("a", 0) or 0), 3),
        })
    if sort == "active":
        items.sort(key=lambda x: x["a"], reverse=True)
    else:
        items.sort(key=lambda x: x["created"], reverse=True)
    total = len(items)
    return total, items[offset:offset + limit]


class Handler(BaseHTTPRequestHandler):
    server_version = "ShihaiConsole/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _check_access(self) -> bool:
        if not ACCESS_TOKEN:
            return True
        if self.headers.get("X-Token") == ACCESS_TOKEN:
            return True
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("token", [""])[0] == ACCESS_TOKEN:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            if part.strip() == "shihai_token=" + ACCESS_TOKEN:
                return True
        return False

    def _send(self, code: int, payload, ctype="application/json; charset=utf-8"):
        raw = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if ACCESS_TOKEN:
            self.send_header("Set-Cookie",
                             f"shihai_token={ACCESS_TOKEN}; Path=/; Max-Age=31536000")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def do_GET(self):
        if not self._check_access():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if not HTML_FILE.exists():
                self._send(500, {"status": "error", "message": "找不到观测台 HTML"})
                return
            raw = HTML_FILE.read_bytes()
            self._send(200, raw, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path.startswith("/assets/"):
            target = (BASE / path.lstrip("/")).resolve()
            if BASE.resolve() in target.parents and target.is_file():
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send(404, {"status": "error", "message": "not found"})
            return
        if path in ("/api/ping", "/ping"):
            self._send(200, {"code": 0, "msg": "pong"})
            return
        if path == "/api/state":
            try:
                status, payload = call_plugin("GET", "state")
                self._send(200, {"status": "ok", "link": "astrbot", "payload": payload})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "link": "down", "message": str(e)})
            return
        if path == "/api/events":
            rows = []
            try:
                trace = ROOT / "data" / "plugin_data" / "shihai" / "guard_trace.jsonl"
                if trace.exists():
                    with open(trace, "r", encoding="utf-8-sig") as fh:
                        for line in fh.readlines()[-40:]:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rows.append(json.loads(line))
                            except ValueError:
                                continue
                self._send(200, {"status": "ok", "rows": rows})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "rows": rows})
            return
        if path == "/api/logs":
            try:
                rows = _log_rows(160)
                self._send(200, {"status": "ok", "rows": rows, "file": LOG_FILE.name})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "rows": []})
            return
        if path == "/api/link":
            try:
                self._send(200, {"status": "ok", "link": _link_state()})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e)})
            return
        if path == "/api/filter_stats":
            self._send(200, {"status": "ok", "filter": SMS_FILTER.snapshot()})
            return
        if path == "/api/memories":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = qs.get("q", [""])[0]
            sort = qs.get("sort", ["created"])[0]
            try:
                limit = min(100, max(1, int(qs.get("limit", ["30"])[0])))
                offset = max(0, int(qs.get("offset", ["0"])[0]))
            except ValueError:
                limit, offset = 30, 0
            try:
                total, page = _memory_items(q, limit, offset, sort)
                self._send(200, {"status": "ok", "total": total,
                                  "count": len(page), "offset": offset, "items": page})
            except Exception as e:  # 文件被占用/损坏时返回错误而非 500
                self._send(200, {"status": "error", "message": str(e),
                                  "total": 0, "items": []})
            return
        if path == "/api/cron_defaults":
            self._send(200, {"status": "ok", "session": _default_session()})
            return
        if path == "/api/tasks":
            try:
                self._send(200, {"status": "ok", "data": _read_json_file(TASKS_FILE)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "data": {"tasks": []}})
            return
        if path == "/api/task_context":
            try:
                self._send(200, {"status": "ok", "data": _read_json_file(TASK_CONTEXT_FILE)})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "data": {}})
            return
        if path == "/api/cron/jobs":
            query = urllib.parse.urlparse(self.path).query
            try:
                code, data = call_dashboard("GET", "/api/cron/jobs", query=query)
                self._send(200, data)
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "data": []})
            return
        if path == "/api/persona":
            try:
                code, data = call_dashboard("GET", "/api/persona/list")
                self._send(200, data)
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e), "data": []})
            return
        self._send(404, {"status": "error", "message": "not found"})

    def do_POST(self):
        if not self._check_access():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        path = self.path.split("?")[0]
        if path in ("/api/ingest", "/sms"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                body = json.loads(raw or "{}")
            except Exception:  # noqa: BLE001
                body = {}
            frm = str(
                body.get("from") or body.get("sender") or
                body.get("phone") or body.get("FROM_NUM") or ""
            ).strip()
            content = str(
                body.get("text") or body.get("msg") or body.get("content") or
                body.get("message") or body.get("SMS_TXT") or ""
            ).strip()
            if not content:
                self._send(200, {"code": 1, "msg": "empty"})
                return
            if SMS_FILTER.enabled():
                ok, reason, content = SMS_FILTER.check(frm, content)
                if not ok:
                    self._send(200, {"code": 2, "msg": "filtered", "reason": reason})
                    return
            line = f"收到来自 {frm} 的短信：{content}" if frm else f"收到短信：{content}"
            _append_action_log(line)
            self._send(200, {"code": 0, "msg": "ok"})
            return
        if path == "/api/cron/jobs":
            try:
                code, data = call_dashboard("POST", "/api/cron/jobs", self._read_body())
                self._send(200, data)
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e)})
            return
        if path == "/api/persona/update":
            try:
                code, data = call_dashboard("POST", "/api/persona/update", self._read_body())
                self._send(200, data)
            except Exception as e:  # noqa: BLE001
                self._send(200, {"status": "error", "message": str(e)})
            return
        if path == "/api/task_context":
            ok, msg, merged = _update_task_context(self._read_body() or {})
            self._send(200, {"status": "ok" if ok else "error",
                              "message": msg, "data": merged})
            return
        if path != "/api/control":
            self._send(404, {"status": "error", "message": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        try:
            status, payload = call_plugin("POST", "cmd", body)
            self._send(200, {"status": "ok", "payload": payload})
        except Exception as e:  # noqa: BLE001
            self._send(200, {"status": "error", "message": str(e)})
            return

    def do_PATCH(self):
        if not self._check_access():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        path = self.path.split("?")[0]
        m = re.match(r"^/api/cron/jobs/(?P<jid>[^/]+)$", path)
        if not m:
            self._send(404, {"status": "error", "message": "not found"})
            return
        try:
            code, data = call_dashboard(
                "PATCH", f"/api/cron/jobs/{urllib.parse.quote(m.group('jid'))}",
                self._read_body())
            self._send(200, data)
        except Exception as e:  # noqa: BLE001
            self._send(200, {"status": "error", "message": str(e)})

    def do_DELETE(self):
        if not self._check_access():
            self._send(401, {"status": "error", "message": "unauthorized"})
            return
        path = self.path.split("?")[0]
        m = re.match(r"^/api/cron/jobs/(?P<jid>[^/]+)$", path)
        if not m:
            self._send(404, {"status": "error", "message": "not found"})
            return
        try:
            code, data = call_dashboard(
                "DELETE", f"/api/cron/jobs/{urllib.parse.quote(m.group('jid'))}")
            self._send(200, data)
        except Exception as e:  # noqa: BLE001
            self._send(200, {"status": "error", "message": str(e)})


def main():
    if not CONFIG_FILE.exists():
        raise SystemExit(f"找不到 AstrBot 配置：{CONFIG_FILE}")
    print(f"[识海控制台] 服务地址 http://{HOST}:{PORT}/")
    print(f"[识海控制台] 数据来源 {ASTRBOT}/api/plug/{PLUGIN}/state")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
