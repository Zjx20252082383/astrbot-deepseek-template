"""
astrbot_plugin_shihai_sms - 识海·信使（短信收件人）

手机短信通过 SmsForwarder 转发到本插件内置 HTTP 服务，
插件解析快递信息（取件码/运单号/驿站/状态），主动发给主人。

流程：
  手机短信 → SmsForwarder App → HTTP POST http://电脑IP:6187/sms
           → 本插件解析 → 快递入库 + 主动私聊上报主人

指令：
  /快递            查看最近的快递信息
  /短信 [条数]     查看最近收到的短信
  /短信测试 <内容>  模拟一条短信，测试解析（不用真发短信）
"""
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime

from aiohttp import web

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain

# ──────────────── 快递解析 ────────────────

# 常见快递/驿站/柜机关键词
CARRIER_RE = re.compile(
    r"(顺丰|京东|中通|圆通|申通|韵达|邮政|EMS|ems|极兔|德邦|菜鸟|丰巢|丹鸟|"
    r"宅急送|跨越|天天快递|百世|裹裹|驿站|快递柜|快递超市|菜鸟驿站|京东柜|丰巢柜)"
)
# 运单号：2~4 个字母开头 + 10~15 位数字（如 SF1388...，YT..., ZTO...，纯数字单号另配）
WAYBILL_RE = re.compile(r"(?<![0-9A-Za-z])([A-Z]{2,4}[0-9]{10,15})(?![0-9A-Za-z])")
# 取件码：各种写法
PICKUP_RE = re.compile(
    r"(?:取件码|提货码|取货码|领件码|凭码|取件号)[码号:：\s]*([0-9A-Za-z\-]{3,15})"
)
# 纯数字取件码兜底：如「凭 8-1-302 取件」
PICKUP_DASH_RE = re.compile(r"(?<![0-9A-Za-z])([0-9]{1,3}-[0-9]{1,2}-[0-9]{2,5})(?![0-9A-Za-z])")
# 状态关键词 → 归一化状态
STATUS_RULES = [
    (re.compile(r"已签收|签收成功|已领取|已取出|已收货"), "已签收"),
    (re.compile(r"正在派送|派送中|即将派送|正在为您派|派件|送货上门|即将送达|马上就到"), "派送中"),
    (re.compile(r"已到|已存放|已投入|已放至|已放入|待取件|请及时领取|代收点|自提柜|驿站"), "到站待取"),
    (re.compile(r"运输中|转运|已发货|已揽收|揽件|正在派送途中|运送"), "运输中"),
]
PHONE_TAIL_RE = re.compile(r"1[3-9]\d{9}|(1[3-9]\d)[*]{4}(\d{4})")


def _now_str() -> str:
    return datetime.now().strftime("%m-%d %H:%M")


class ExpressParser:
    @staticmethod
    def parse(text: str, sender: str) -> dict | None:
        """解析一条短信，是快递相关则返回结构化 dict，否则 None。"""
        if not text:
            return None
        m_carrier = CARRIER_RE.search(text)
        m_waybill = WAYBILL_RE.search(text)
        m_pickup = PICKUP_RE.search(text)
        pickup = ""
        if m_pickup:
            pickup = m_pickup.group(1)
        else:
            m_dash = PICKUP_DASH_RE.search(text)
            if m_dash and m_carrier:
                pickup = m_dash.group(1)
        status = ""
        for pat, name in STATUS_RULES:
            if pat.search(text):
                status = name
                break
        # 判定"快递相关"：至少两类证据同时出现（品牌词+取件码/单号/状态），
        # 或取件码/单号+状态单独成立——聊天里随口提到"顺丰"不算快递短信
        is_express = bool(
            m_pickup
            or (m_waybill and status)
            or (m_carrier and (m_pickup or m_waybill or status))
        )
        if not is_express:
            return None
        sender_clean = sender if sender else ""
        # 收件人尾号
        tail = ""
        m_tail = PHONE_TAIL_RE.search(text)
        if m_tail:
            raw = m_tail.group(0)
            digits = re.sub(r"\D", "", raw)
            tail = digits[-4:] if len(digits) >= 4 else raw
        return {
            "carrier": m_carrier.group(1) if m_carrier else "快递",
            "waybill": m_waybill.group(1) if m_waybill else "",
            "pickup": pickup,
            "status": status or "有新消息",
            "phone_tail": tail,
            "summary": re.sub(r"\s+", " ", text).strip()[:200],
        }


@register("shihai_sms", "zjx", "识海·信使（短信→快递→报主人）", "1.0.0")
class ShihaiSmsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.port = int(config.get("port", 6187))
        self.token = str(config.get("token", "")).strip()
        self.owner_qid = str(config.get("owner_qid", "")).strip()
        self.platform_id = str(config.get("platform_id", "")).strip()
        self.report_express = bool(config.get("report_express", True))
        self.report_other = bool(config.get("report_other", False))
        self.forward_all = bool(config.get("forward_all", True))
        self.keep_days = int(config.get("keep_days", 30))
        self.max_records = int(config.get("max_records", 500))
        self._records: list[dict] = []
        self._seen: set[str] = set()
        self._runner: web.AppRunner | None = None
        # 短信是隐私数据，必须放 plugin_data（插件目录会随 Git 同步，绝不能让短信记录进仓库）
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            data_dir = os.path.join(str(get_astrbot_plugin_data_path()), "shihai_sms")
        except Exception:
            data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(data_dir, exist_ok=True)
        self._store_path = os.path.join(data_dir, "sms_records.json")
        # 旧版数据在插件目录下：存在则搬迁（一次性迁移）
        old_path = os.path.join(os.path.dirname(__file__), "data", "sms_records.json")
        try:
            if os.path.exists(old_path) and not os.path.exists(self._store_path):
                os.replace(old_path, self._store_path)
        except Exception:
            pass
        self._load_records()

    # ──────────────── 生命周期 ────────────────

    async def initialize(self):
        self._start_server()

    async def terminate(self):
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    # ──────────────── 存储 ────────────────

    def _load_records(self):
        try:
            if os.path.exists(self._store_path):
                with open(self._store_path, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
            self._seen = {
                r.get("dedup", "") for r in self._records if r.get("dedup")
            }
        except Exception as e:
            logger.warning(f"[识海信使] 读取记录失败: {e}")
            self._records = []

    def _save_records(self):
        try:
            os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
            cutoff = time.time() - self.keep_days * 86400
            self._records = [
                r for r in self._records
                if r.get("ts", 0) >= cutoff
            ][-self.max_records:]
            with open(self._store_path, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[识海信使] 保存记录失败: {e}")

    def _add_record(self, sender: str, text: str, ts: int, express: dict | None) -> dict | None:
        dedup = hashlib.md5(f"{sender}|{text}|{ts // 60}".encode()).hexdigest()
        if dedup in self._seen:
            return None
        self._seen.add(dedup)
        rec = {
            "dedup": dedup,
            "ts": ts,
            "time": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "text": text,
            "express": express,
        }
        self._records.append(rec)
        self._save_records()
        return rec

    # ──────────────── HTTP 服务 ────────────────

    def _start_server(self):
        app = web.Application()
        app.router.add_post("/sms", self._handle_sms)
        app.router.add_get("/sms", self._handle_sms)
        app.router.add_get("/ping", self._handle_ping)

        async def _boot():
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "0.0.0.0", self.port)
            await site.start()
            logger.info(f"[识海信使] 短信接收服务已启动：http://0.0.0.0:{self.port}/sms")

        self._server_task = asyncio.ensure_future(_boot())
        self._server_task.add_done_callback(self._on_server_done)

    @staticmethod
    def _on_server_done(task: asyncio.Task):
        if not task.cancelled() and task.exception():
            logger.error(f"[识海信使] HTTP 服务启动失败: {task.exception()}（端口 {task} 可能被占用）")

    def _check_token(self, request: web.Request, body_token: str = "") -> bool:
        if not self.token:
            logger.warning("[识海信使] 未设置 token——局域网内任何人都能向本机伪造短信请求，建议在配置里设一个")
            return True
        given = (request.headers.get("X-Token") or request.query.get("token", "") or body_token or "").strip()
        return given == self.token

    async def _handle_ping(self, request: web.Request):
        return web.json_response({"code": 0, "msg": "pong", "time": _now_str()})

    async def _handle_sms(self, request: web.Request):
        """兼容 SmsForwarder WebHook 的 JSON / 表单 / GET 请求。"""
        try:
            if request.method == "GET":
                data = {k: request.query.get(k, "") for k in ("from", "text", "channel", "timestamp")}
                body_token = ""
            else:
                ct = (request.headers.get("Content-Type") or "").lower()
                if "json" in ct:
                    payload = await request.json()
                    data = payload if isinstance(payload, dict) else {}
                    body_token = str(data.get("token", ""))
                else:
                    form = await request.post()
                    data = dict(form)
                    body_token = str(data.get("token", ""))
        except Exception as e:
            logger.warning(f"[识海信使] 解析请求失败: {e}")
            return web.json_response({"code": 1, "msg": "bad request"})

        if not self._check_token(request, body_token):
            return web.json_response({"code": 2, "msg": "token 校验失败"})

        sender = str(data.get("from") or data.get("mobile") or data.get("address") or "").strip()
        text = str(data.get("text") or data.get("content") or data.get("message") or "").strip()
        channel = str(data.get("channel") or data.get("type") or "sms").strip()
        ts_raw = data.get("timestamp") or data.get("date") or 0
        try:
            ts = int(float(ts_raw))
            if ts > 1e12:  # 毫秒转秒
                ts //= 1000
            if ts <= 0 or ts > time.time() + 3600:
                ts = int(time.time())
        except (TypeError, ValueError):
            ts = int(time.time())

        if not text:
            return web.json_response({"code": 0, "msg": "empty"})

        express = ExpressParser.parse(text, sender) if channel in ("sms", "") else None
        if express is None and not self.forward_all:
            return web.json_response({"code": 0, "msg": "skipped"})
        rec = self._add_record(sender, text, ts, express)
        if rec is None:
            return web.json_response({"code": 0, "msg": "duplicate"})

        logger.info(f"[识海信使] 收到短信 [{channel}] from={sender}: {text[:60]}")
        try:
            await self._report(rec, express)
        except Exception as e:
            logger.warning(f"[识海信使] 上报失败: {type(e).__name__} {e}")
        return web.json_response({"code": 0, "msg": "ok"})

    # ──────────────── 上报主人 ────────────────

    async def _report(self, rec: dict, express: dict | None):
        text = self._format_express(rec) if express else (
            f"主人，收到一条短信（{_now_str()}）\n来自 {rec['sender']}：{rec['text'][:120]}"
            if self.report_other else ""
        )
        if not text:
            return
        target = self._owner_target()
        if not target:
            logger.warning("[识海信使] 未配置 owner_qid / platform_id，无法上报")
            return
        chain_text = text
        try:
            from astrbot.api.event import MessageChain
            ok = await self.context.send_message(target, MessageChain(chain=[Plain(text=chain_text)]))
            if ok is False:
                raise RuntimeError("send_message 返回 False")
        except Exception:
            # 兜底：直接传字符串
            await self.context.send_message(target, chain_text)

    def _owner_target(self) -> str:
        if self.owner_qid and self.platform_id:
            return f"{self.platform_id}:FriendMessage:{self.owner_qid}"
        return ""

    def _format_express(self, rec: dict) -> str:
        e = rec["express"]
        lines = [f"主人，有快递消息啦（{_now_str()}）"]
        head = f"📦 {e['carrier']}"
        if e["waybill"]:
            head += f" · 单号 {e['waybill']}"
        lines.append(head)
        if e["status"]:
            lines.append(f"状态：{e['status']}")
        if e["pickup"]:
            lines.append(f"取件码：{e['pickup']}")
        if e["phone_tail"]:
            lines.append(f"收件手机尾号：{e['phone_tail']}")
        lines.append(f"原文：{e['summary']}")
        return "\n".join(lines)

    # ──────────────── QQ 指令 ────────────────

    @filter.command("快递")
    async def cmd_express(self, event: AstrMessageEvent):
        """查看最近的快递信息"""
        express_recs = [r for r in self._records if r.get("express")]
        if not express_recs:
            yield event.plain_result("最近还没有收到过快递短信哦～")
            return
        recs = express_recs[-8:]
        lines = ["📦 最近快递（新→旧）："]
        for r in reversed(recs):
            e = r["express"]
            line = f"{r['time'][:16]} {e['carrier']}"
            if e["waybill"]:
                line += f" {e['waybill']}"
            if e["status"]:
                line += f" [{e['status']}]"
            if e["pickup"]:
                line += f" 取件码 {e['pickup']}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    @filter.command("短信")
    async def cmd_sms(self, event: AstrMessageEvent, count: str = "5"):
        """查看最近收到的短信"""
        try:
            n = max(1, min(20, int(count)))
        except ValueError:
            n = 5
        if not self._records:
            yield event.plain_result("还没有收到过转发的短信哦～")
            return
        lines = ["📨 最近短信："]
        for r in reversed(self._records[-n:]):
            tag = "📦" if r.get("express") else "💬"
            preview = r["text"][:80]
            lines.append(f"{tag} {r['time'][:16]} 来自{r['sender']}：{preview}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("短信测试")
    async def cmd_sms_test(self, event: AstrMessageEvent, content: str = ""):
        """模拟一条短信测试解析，如：/短信测试 顺丰快递：您的快递已到菜鸟驿站，取件码8-1-302"""
        if not content:
            yield event.plain_result("用法：/短信测试 <短信内容>")
            return
        express = ExpressParser.parse(content, "TEST")
        rec = self._add_record("TEST", content, int(time.time()), express)
        if express:
            yield event.plain_result(self._format_express(rec) + "\n（测试模式，未真实上报）")
        else:
            yield event.plain_result("未识别出快递信息（会作为普通短信存档）。")

    # ──────────────── LLM 工具 ────────────────

    @filter.llm_tool(name="get_recent_express")
    async def tool_get_recent_express(self, event: AstrMessageEvent):
        """查询主人最近收到的快递信息（取件码、运单号、快递状态）。
        当主人问「有没有快递」「我的包裹到哪了」「取件码是多少」等时调用本工具。"""
        express_recs = [r for r in self._records if r.get("express")]
        if not express_recs:
            return "最近 30 天内没有收到任何快递相关的短信。"
        out = []
        for r in express_recs[-10:]:
            e = r["express"]
            bits = [r["time"][:16], e["carrier"]]
            if e["waybill"]:
                bits.append(f"单号{e['waybill']}")
            if e["status"]:
                bits.append(e["status"])
            if e["pickup"]:
                bits.append(f"取件码{e['pickup']}")
            out.append(" | ".join(bits))
        return "\n".join(out)
