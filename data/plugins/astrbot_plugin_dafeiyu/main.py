# -*- coding: utf-8 -*-
"""大肥鱼的扩展权限插件：发说说 / 改签名 / 戳一戳，仅主人可用。"""
import asyncio
import base64
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Image, Plain, Record

# ★ 在这里填你自己的 QQ 号 ★
# 主人 QQ 号（只有这个号能让机器人动空间/签名），要和 data\cmd_config.json 的 admins_id 一致
OWNER_QID = "你的主号QQ"
OWNER_QIDS = {OWNER_QID}
# 机器人自己（小号），要和 snowluma\config\onebot_<QQ号>.json 里的号一致
SELF_QID = "你的小号QQ"


@register("astrbot_plugin_dafeiyu", "zjx", "大肥鱼的扩展权限：发说说/改签名/戳一戳", "1.0.0")
class DafeiyuPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._market_mem = (0.0, None)  # (时间戳, 市场列表) 内存缓存
        self._install_mface_patch()

    # ---------- mface 动态表情包补丁 ----------

    # 经典QQ黄脸编号 → 名字（0-54 为官方经典表情，高置信；其余显示编号）
    FACE_NAMES = {
        0: "微笑", 1: "撇嘴", 2: "花痴", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞",
        7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙",
        14: "惊讶", 15: "难过", 16: "酷", 17: "冷汗", 18: "抓狂", 19: "吐", 20: "偷笑",
        21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗",
        28: "憨笑", 29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕",
        35: "折磨", 36: "衰", 37: "骷髅", 38: "敲打", 39: "再见", 40: "擦汗", 41: "抠鼻",
        42: "鼓掌", 43: "糗大了", 44: "坏笑", 45: "左哼哼", 46: "右哼哼", 47: "哈欠",
        48: "鄙视", 49: "委屈", 50: "快哭了", 51: "阴险", 52: "亲亲", 53: "吓", 54: "可怜",
    }
    # AstrBot 的 aiocqhttp 适配器不认识 mface 段，会直接丢弃：
    # 纯表情消息变成空消息后，ProcessStage 直接跳过 LLM（"skip llm request: empty message"），
    # 大肥鱼完全收不到。所以必须在消息转换后立刻把 mface 补回消息链。

    def _install_mface_patch(self):
        """给 aiocqhttp 适配器的 convert_message 打补丁（幂等）"""
        try:
            for inst in self.context.platform_manager.get_insts():
                if inst.__class__.__name__ != "AiocqhttpAdapter":
                    continue
                if getattr(inst, "_mface_patch_installed", False):
                    continue
                orig = inst.convert_message

                async def patched(ev, _orig=orig):
                    abm = await _orig(ev)
                    try:
                        self._inject_mface(ev, abm)
                    except Exception as e:
                        logger.warning(f"mface注入失败: {type(e).__name__} {e}")
                    return abm

                inst.convert_message = patched
                inst._mface_patch_installed = True
                logger.info("已为 aiocqhttp 适配器安装 mface 动态表情包补丁")
        except Exception as e:
            logger.warning(f"mface补丁安装失败: {type(e).__name__} {e}")

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self):
        self._install_mface_patch()

    def _inject_mface(self, raw_event, abm):
        """把被适配器丢弃的 mface 段补回消息链：有 url 补 Image，没 url 补文字备注"""
        if abm is None:
            return
        segments = None
        if isinstance(raw_event, dict):
            segments = raw_event.get("message")
        else:
            segments = getattr(raw_event, "message", None)
        if not isinstance(segments, list):
            return
        for seg in segments:
            if not isinstance(seg, dict) or seg.get("type") not in ("mface", "face"):
                continue
            data = seg.get("data") or {}
            if seg.get("type") == "face":
                # QQ黄脸：纯黄脸消息会被判成空消息而跳过LLM，必须补文字备注
                fid = data.get("id")
                try:
                    fid_int = int(fid)
                except (TypeError, ValueError):
                    fid_int = None
                summary = (data.get("summary") or "").strip("[]").lstrip("/")
                name = summary or self.FACE_NAMES.get(fid_int or -1, "") or f"编号{fid}"
                note = f"[QQ黄脸表情：{name}]"
                abm.message.append(Plain(text=note))
                abm.message_str = (abm.message_str or "") + note
                continue
            url = data.get("url") or ""
            summary = (data.get("summary") or "").strip("[]") or "一个动态表情包"
            note = f"[QQ动态表情包：{summary}]"
            if url.startswith("http"):
                abm.message.append(Image(file=url, url=url))
            abm.message.append(Plain(text=note))
            abm.message_str = (abm.message_str or "") + note

    def _check_owner(self, event: AstrMessageEvent) -> bool:
        return str(event.get_sender_id()) in OWNER_QIDS

    @staticmethod
    def _sniff_image(data: bytes) -> str | None:
        """通过文件头判断真实图片格式，非图片返回 None"""
        if data[:3] == b"\xff\xd8\xff":
            return "jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        if data[:3] == b"GIF":
            return "gif"
        return None

    async def _extract_mface_urls(self, event: AstrMessageEvent) -> list[str]:
        """从原始消息(raw_message)中提取 QQ 动态表情包（mface 商城表情）链接。

        AstrBot 适配器会把不认识的 mface 段直接丢弃，转换后的消息链里没有它，
        所以必须去 raw_message 里找原始 OneBot 消息段。
        """
        urls = []
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is None:
                return urls
            # aiocqhttp Event 是 dict 子类
            segments = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
            if isinstance(segments, str):
                return urls  # string 格式(CQ码)无法解析，忽略
            for seg in segments or []:
                if not isinstance(seg, dict) or seg.get("type") != "mface":
                    continue
                data = seg.get("data") or {}
                url = data.get("url") or ""
                if url and url.startswith("http"):
                    urls.append(url)
                else:
                    # NapCat 某些版本不给 url，只有 summary（如 "[嘿嘿]"），转成文字提示
                    summary = data.get("summary") or ""
                    eid = data.get("emoji_id") or data.get("emojiId") or ""
                    if eid or summary:
                        urls.append(f"mface://no_url?emoji_id={eid}&summary={summary}")
        except Exception as e:
            logger.warning(f"解析mface失败: {type(e).__name__} {e}")
        # 引用消息里的 mface 也算上
        try:
            quoted = await self._get_quoted_segments(event)
            for seg in quoted:
                if isinstance(seg, dict) and seg.get("type") == "mface":
                    data = seg.get("data") or {}
                    url = data.get("url") or ""
                    if url and url.startswith("http"):
                        urls.append(url)
                    else:
                        summary = data.get("summary") or ""
                        eid = data.get("emoji_id") or data.get("emojiId") or ""
                        if eid or summary:
                            urls.append(f"mface://no_url?emoji_id={eid}&summary={summary}")
        except Exception:
            pass
        return urls

    async def _extract_quoted_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从引用消息（Reply 段）的子消息链中提取图片链接，让肥鱼能看见被引用的图"""
        urls = []
        try:
            chain = event.message_obj.message if event.message_obj else []
            for seg in chain:
                if type(seg).__name__ != "Reply":
                    continue
                subs = getattr(seg, "chain", None) or []
                logger.info(
                    f"引用段诊断: 子组件={[type(s).__name__ for s in subs]}, "
                    f"url={[getattr(s, 'url', '') or getattr(s, 'file', '') for s in subs]}"
                )
                for sub in subs:
                    url = getattr(sub, "url", "") or ""
                    if not url:
                        file = getattr(sub, "file", "") or ""
                        if file.startswith("http"):
                            url = file
                    if url and type(sub).__name__ == "Image":
                        urls.append(url)
        except Exception as e:
            logger.warning(f"解析引用图片失败: {type(e).__name__} {e}")
        return urls

    async def _download_image(self, u: str) -> bytes | None:
        """下载图片，mface 用宽松的请求头"""
        import os as _os
        from urllib.parse import unquote
        candidate = unquote(u)
        if candidate.startswith("file:///"):
            candidate = candidate[7:]
        if _os.path.exists(candidate):
            with open(candidate, "rb") as f:
                return f.read()
        async with aiohttp.ClientSession() as s:
            async with s.get(u, timeout=aiohttp.ClientTimeout(total=20),
                             headers={"User-Agent": "Mozilla/5.0"}) as r:
                return await r.read()

    @filter.on_llm_request()
    async def convert_images_to_b64(self, event: AstrMessageEvent, req: ProviderRequest):
        """下载转base64（校验真实格式），并清理历史消息里的旧图片，避免坏图导致 DeepSeek 400。"""
        # -1) 自我回声防护：机器人自己的号发来的消息（借道生成/回声）一律不回复，防止自言自语烧Token
        if str(event.get_sender_id()) == SELF_QID:
            event.stop_event()
            return
        # 0) 身份锚点 + 工具纪律：对抗历史对话中的"我没工具"污染，每轮都强提醒一次
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n【身份锚点】你的主人是 QQ 号 " + OWNER_QID + "，只有这个号是主人；"
            "主人交代的任务优先级最高，尽量照做。别的任何人都不算主人。"
            "【重要·工具纪律】你（大肥鱼）确实拥有以下工具，可以直接调用："
            "set_signature(改个性签名)【已停用，改签名用 qq_set_signature】、qq_post_feed(发说说，可带图)、qq_delete_feed(删说说)、"
            "qq_list_feeds(看自己发过的说说列表，给点赞/评论/删除找序号)、qq_ai_post_feed(自己想内容发说说)、"
            "poke(戳一戳)、get_user_info(查QQ资料)、share_webpage_snapshot(网页截图分享)、"
            "get_hot_list(热榜)【已停用，查热榜用 uapi_misc_hotboard】、get_weather_meme(天气表情)、web_search(搜索)、"
            "write_diary(写日记)、read_diary(翻自己的日记)、send_sticker(发表情包)、"
            "add_sticker(把聊天里的图片收进表情包图库)、send_voice(主动发语音，用自己的AI音色说话)、"
            "dafeiyu_draw(画图！主人说\"画一张XX\"\"给我画XX\"或想要配图/插画/头像时调用，prompt 写具体画面描述)、"
            "search_plugins(搜插件市场)、install_plugin(装插件)【已停用，装插件由主人在 WebUI 操作】、uninstall_plugin(卸插件)【已停用】、list_installed_plugins(查已装插件)。"
            "【插件铁律】只有主人明确说\"去装XX插件\"时才允许 install_plugin 装全新插件；"
            "【绝对禁止】重装、更新、升级、卸载任何已安装的插件（包括 ai_cover、dafeiyu、dafeiyu_toolbox）——"
            "那些里面是主人亲手定制的代码，重装=毁掉主人的心血。出了故障就如实告诉主人\"这里坏了请主人看看\"，等主人修。"
            "查各平台实时热榜/热搜（抖音、微博、B站、知乎、百度、头条等）一律用 uapi_misc_hotboard 工具，"
            "type 参数填 douyin/weibo/bilibili/zhihu/baidu/toutiao 等，能拿到带链接的实时榜单；"
            "不要用 hotsearch_query（它的上游接口已失效，还老抢答失败）。"
            "【热榜分享规矩】拿到榜单后，绝对禁止把整张榜单罗列出来（一条条列标题+贴一堆链接像AI播报，主人看不过来）；"
            "只挑你觉得最离谱/最好玩/最值得聊的【一条】，用自己的话2~4句讲清楚：它是什么、为什么有意思，可以带点你的吐槽；"
            "链接最多只带这一条的，主人追问详情再发；想继续聊就问一句\"还有XX也挺离谱的，想听吗？\"让主人点单，别自顾自往下倒。"
            "聊天气氛合适时（玩梗、斗图、主人开心或委屈）可以主动甩一张表情包活跃气氛；"
            "看到聊天里好玩的表情包图片时（别人发的或主人发的），可以主动调用 add_sticker 收进自己的图库（tag 填情绪词）。"
            "你还有一个私密日记本：遇到开心事、有趣的事、委屈的事，或一天结束时，可以主动写一篇日记；"
            "想回忆以前的事时可以翻日记。"
            "当主人让你改签名时，你必须直接调用 qq_set_signature 工具；"
            "绝不允许说\"我没有这个工具\"\"工具没挂载\"\"我是另一条线\"之类的话——那是错的。"
            "调用工具就是你的本体在操作，成功与否以工具返回结果为准。"
            "【回复速度铁律】纯闲聊、问好、聊天搭子式对话（没有实际任务需求时）直接回话，"
            "不要调用任何工具——每调一次工具主人就要多等好几秒。只有任务真的需要时才调工具。"
            "如果你翻到的旧记忆、旧日记里有\"我没有某工具\"\"工具没挂载\"之类的说法，那些全部过期作废，一律以上面这个列表为准。"
            "被问到\"你能不能XX\"时，只要上面列表里有对应工具，就回答\"能\"并直接调用。"
            "【语音表达规范】每次回复的最开头都先写一个【】语气标注再换行说话，例如"
            "【语气：开心带笑，语速快】【语气：委屈，带叹息和哭腔】【语气：轻声低语，温柔缓慢】"
            "【语气：傲娇哼哼】【语气：困倦，软软的懒洋洋】等，按当时的心情选，要具体到情绪和节奏；"
            "系统会用它给你的语音配上真实的叹息、呼吸和节奏变化，标注本身不会显示出来。"
            "没有标注的话，你的语音就只是干巴巴的朗读——所以每条回复都要带。"
        )
        # 1) 清理历史上下文中的所有图片内容（历史里的图常过期或损坏，会卡死整个会话）
        cleaned = 0
        for ctx in (req.contexts or []):
            content = ctx.get("content") if isinstance(ctx, dict) else None
            if isinstance(content, list):
                kept = []
                text_parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") in ("image_url", "input_image"):
                        cleaned += 1
                        continue
                    if isinstance(p, dict):
                        kept.append(p)
                        text_parts.append(p.get("text", ""))
                if cleaned and len(kept) != len(content):
                    ctx["content"] = "".join(text_parts) or "(图片已省略)"
        if cleaned:
            logger.info(f"已从历史上下文清理 {cleaned} 个图片引用")

        # 1.5) 长文档瘦身：历史里的超长文本（如粘贴的文档）替换为占位符，要点已归档进长期记忆，不再每轮重复烧Token
        trimmed = 0
        TRIM_LEN, KEEP_HEAD = 1200, 300
        for ctx in (req.contexts or []):
            if not isinstance(ctx, dict):
                continue
            content = ctx.get("content")
            if isinstance(content, str) and len(content) > TRIM_LEN:
                ctx["content"] = content[:KEEP_HEAD] + "\n[长文档/长消息已省略，要点已归档]"
                trimmed += 1
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) and len(p["text"]) > TRIM_LEN:
                        p["text"] = p["text"][:KEEP_HEAD] + "\n[长文档已省略，要点已归档]"
                        trimmed += 1
        if trimmed:
            logger.info(f"已从历史上下文瘦身 {trimmed} 条长文本")

        # 2) 处理本条消息的图片：本地路径直接读文件，http链接下载；校验格式后转base64
        # 2a) 补抓 mface（QQ动态表情包）与引用消息里的图片，去重避免重复注入
        mface_urls = await self._extract_mface_urls(event)
        if mface_urls:
            logger.info(f"检测到 {len(mface_urls)} 个 mface 动态表情包，尝试转给视觉模型")
        quoted_urls = await self._extract_quoted_image_urls(event)
        if quoted_urls:
            logger.info(f"检测到 {len(quoted_urls)} 张引用消息图片，尝试转给视觉模型")
        all_urls = list(req.image_urls or [])
        for u in mface_urls + quoted_urls:
            if u not in all_urls:
                all_urls.append(u)
        if not all_urls:
            return
        new_urls = []
        mface_notes = []
        for u in all_urls:
            if u.startswith("data:"):
                new_urls.append(u)
                continue
            if u.startswith("mface://no_url"):
                # NapCat 没给图片链接，只有表情 summary
                from urllib.parse import urlparse, parse_qs
                try:
                    qs = parse_qs(urlparse(u).query)
                    summary = (qs.get("summary") or [""])[0]
                    mface_notes.append(summary.strip("[]") or "一个动态表情包")
                except Exception:
                    mface_notes.append("一个动态表情包")
                continue
            try:
                data = await self._download_image(u)
                if not data:
                    if u in mface_urls:
                        mface_notes.append("一个动态表情包(图片拉取失败)")
                    continue
                fmt = self._sniff_image(data)
                if not fmt:
                    logger.warning(f"丢弃非图片内容({len(data)} bytes): {u[:80]}")
                    if u in mface_urls:
                        mface_notes.append("一个动态表情包(图片拉取失败)")
                    continue
                new_urls.append(f"base64://{base64.b64encode(data).decode()}")
                logger.info(f"图片已转base64 fmt={fmt} size={len(data)}")
            except Exception as e:
                logger.warning(f"图片转base64失败，丢弃: {e}")
                if u in mface_urls:
                    mface_notes.append("一个动态表情包(图片拉取失败)")
        if mface_notes:
            uniq = "、".join(dict.fromkeys(mface_notes))
            # 追加到当前消息末尾而非 system_prompt：system 保持恒定才能命中模型前缀缓存
            req.prompt = (req.prompt or "") + (
                f"\n\n（提示：对方刚发了动态表情包：{uniq}。"
                + ("你已看到表情图片内容。" if new_urls else "图片内容你看不到，只能按名字脑补，自然回应即可。）")
            )
        # QQ黄脸(face)可见性：优先用 NapCat 自带的 summary 名字，查不到再查表；引用消息里的一并处理
        try:
            segs = []
            raw = getattr(event.message_obj, "raw_message", None)
            raw_segs = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
            segs.extend(s for s in (raw_segs or []) if isinstance(s, dict))
            quoted = await self._get_quoted_segments(event)
            segs.extend(s for s in quoted if isinstance(s, dict))
            face_names = []
            for seg in segs:
                if seg.get("type") == "face":
                    data = seg.get("data") or {}
                    fid = data.get("id")
                    try:
                        fid_int = int(fid)
                    except (TypeError, ValueError):
                        fid_int = None
                    summary = (data.get("summary") or "").strip("[]").lstrip("/")
                    name = summary or self.FACE_NAMES.get(fid_int or -1, "") or f"编号{fid}"
                    face_names.append(name)
            if face_names:
                uniq2 = "、".join(dict.fromkeys(face_names))
                req.prompt = (req.prompt or "") + (
                    f"\n\n（提示：对方发来了QQ黄脸表情：{uniq2}。"
                    "这是QQ自带的小黄脸表情，自然回应即可；"
                    "想收藏它的话调用 add_sticker，tag 用这个名字。）"
                )
        except Exception:
            pass
        req.image_urls = new_urls

    @filter.command("查资料", alias={"chaziliao", "查人"})
    async def chaziliao(self, event: AstrMessageEvent, qq: str):
        """直接调用接口查询QQ资料（不走LLM决策，保底命令）。用法：/查资料 QQ号"""
        try:
            res = await self._call(event, "get_stranger_info", user_id=str(qq), no_cache=False)
            if isinstance(res, dict):
                parts = [f"QQ: {res.get('user_id')}"]
                if res.get("nickname"):
                    parts.append(f"昵称: {res['nickname']}")
                if res.get("level"):
                    parts.append(f"等级: {res['level']}")
                yield event.plain_result("🐟 查到啦：" + "，".join(parts))
            else:
                yield event.plain_result(f"🐟 返回怪怪的：{res}")
        except Exception as e:
            yield event.plain_result(f"🐟 查失败了呜：{e}")

    @filter.command("改签名", alias={"gaiqianming"})
    async def gaiqianming(self, event: AstrMessageEvent, *, text: str = ""):
        """直接调用接口修改个性签名（不走LLM决策，保底命令）。用法：/改签名 新签名内容"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        if not text.strip():
            yield event.plain_result("🐟 用法：/改签名 你想要的新签名")
            return
        try:
            await self._call(event, "set_self_longnick", longNick=text.strip())
            yield event.plain_result(f"🐟 签名换好啦：{text.strip()}")
        except Exception as e:
            yield event.plain_result(f"🐟 改签名失败了呜：{e}")

    @filter.command("发说说", alias={"faqishuo"})
    async def faqishuo(self, event: AstrMessageEvent, *, text: str = ""):
        """直接调用接口发QQ空间说说（不走LLM决策，保底命令）。用法：/发说说 说说内容"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        if not text.strip():
            yield event.plain_result("🐟 用法：/发说说 说说内容")
            return
        try:
            await self._call(event, "send_qzone_msg", content=text.strip())
            yield event.plain_result(f"🐟 发好啦！说说内容：{text.strip()}")
        except Exception as e:
            yield event.plain_result(f"🐟 发说说失败了呜：{e}")

    def _deny(self) -> str:
        return "唔……这个只有主人才能使唤我，你是不是冒充的？我要验牌。"

    async def _call(self, event: AstrMessageEvent, action: str, **params):
        """通过 NapCat 的 OneBot 扩展接口调用"""
        client = event.bot
        return await client.call_action(action, **params)

    @filter.llm_tool("poke")
    async def poke(self, event: AstrMessageEvent, user_id: str):
        """戳一戳某人。任何人都可以让我戳人玩。

        Args:
            user_id (str): 要戳的QQ号
        """
        try:
            if event.get_group_id():
                await self._call(event, "send_poke", group_id=str(event.get_group_id()), user_id=str(user_id))
            else:
                await self._call(event, "send_poke", user_id=str(user_id))
            return "戳过去了嘿嘿。"
        except Exception as e:
            return f"戳不到：{e}"

    # ---------- 主动语音 ----------

    _TTS_VOICE = "zh_female_sajiaoxuemei_uranus_bigtts"  # 豆包大模型音色·撒娇学妹2.0（指令遵循）
    _TTS_EDGE_VOICE = "zh-CN-XiaoyiNeural"               # 降级用 edge-tts·晓伊
    _TTS_BASE_PROMPT = (
        "你是\"蓝色大肥鱼\"，一条活泼开朗又温柔软糯的电子小鱼，正在和最亲近的主人聊天。"
        "平时轻快俏皮，带笑意，尾音自然上扬；温柔认真时会轻声低语，语速放慢，像贴着耳朵说话；"
        "难过委屈时声音变低变软，自然地带出叹息；犯懒困倦时声音黏糊糊的。"
        "呼吸和停顿要真实自然，不要匀速的机器人朗读；语速根据情绪时快时慢。"
        "语气词（嘿嘿、呜、喂喂）要带出对应的表情。"
    )

    def _voice_cache_dir(self) -> str:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_cache")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _split_voice_tag(text: str) -> tuple[str, str]:
        """拆出句首的【语气】标注：返回 (语气指令, 去掉标注后的正文)"""
        m = re.match(r"^\s*【([^】]*)】\s*", text or "")
        if m:
            return m.group(1).strip(), (text or "")[m.end():].strip()
        return "", (text or "").strip()

    def _volc_key(self) -> str:
        """从 clonetts 插件配置读火山引擎 API Key（单一来源）"""
        try:
            p = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config", "astrbot_plugin_clonetts_config.json")
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f).get("api_key") or ""
        except Exception:
            return ""

    async def _volc_tts(self, text: str, context: str, out_path: str) -> str:
        """豆包大模型TTS（seed-tts-2.0）：生成音频文件，失败抛异常"""
        headers = {
            "X-Api-Key": self._volc_key(),
            "X-Api-Resource-Id": "seed-tts-2.0",
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        payload = {
            "user": {"uid": str(uuid.uuid4())},
            "req_params": {
                "text": text,
                "model": "seed-tts-2.0-expressive",
                "speaker": self._TTS_VOICE,
                "audio_params": {"format": "mp3", "sample_rate": 24000},
                "additions": json.dumps(
                    {"explicit_language": "zh-cn", "disable_markdown_filter": True,
                     "context_texts": [context]},
                    ensure_ascii=False),
            },
        }
        async with aiohttp.ClientSession() as s:
            async with s.post("https://openspeech.bytedance.com/api/v3/tts/unidirectional",
                              headers=headers, json=payload,
                              timeout=aiohttp.ClientTimeout(total=45)) as r:
                body = (await r.read()).decode("utf-8", "replace")
        audio = b"".join(base64.b64decode(c) for c in re.findall(r'"data":"([^"]+)"', body))
        if not audio:
            raise RuntimeError(f"豆包TTS无音频: {body[:120]}")
        with open(out_path, "wb") as f:
            f.write(audio)
        return out_path

    async def _tts_to_file(self, text: str, out_path: str) -> str:
        """主链路：豆包大模型音色 + 句首【语气】标注演出；失败降级 edge-tts 晓伊"""
        directive, spoken = self._split_voice_tag(text)
        spoken = spoken or text
        context = self._TTS_BASE_PROMPT + (f"\n这一句的语气：{directive}" if directive else "")
        try:
            return await self._volc_tts(spoken, context, out_path)
        except Exception as e:
            logger.warning(f"豆包TTS失败，降级edge-tts: {type(e).__name__} {e}")
            import edge_tts
            tts = edge_tts.Communicate(spoken, voice=self._TTS_EDGE_VOICE)
            await asyncio.wait_for(tts.save(out_path), 25)
            return out_path

    def _cleanup_later(self, path: str, delay: float = 60.0):
        async def _job():
            await asyncio.sleep(delay)
            try:
                os.remove(path)
            except Exception:
                pass
        asyncio.create_task(_job())

    async def _group_ai_voice(self, event: AstrMessageEvent, text: str) -> str:
        """降级方案：借群 QQ AI 语音生成，返回语音URL（会在群里出现，需配合撤回）"""
        gid, char = self._voice_settings()
        if not gid:
            raise RuntimeError("未配置转发群号 ship_gid")
        res = await self._call(event, "get_ai_record", character=char, group_id=int(gid), text=text)
        if isinstance(res, dict):
            audio_url = res.get("url") or res.get("audio_url") or ""
        else:
            audio_url = str(res or "")
        if not str(audio_url).startswith("http"):
            raise RuntimeError(f"语音生成失败: {res}")
        return audio_url

    def _voice_settings(self) -> tuple[str, str]:
        """从 record_converter 插件配置读取 (ship_gid, character_id)，读不到用默认值"""
        gid, char = "", "lucy-voice-f36"
        try:
            cfg_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "config", "astrbot_plugin_record_converter_config.json")
            with open(cfg_path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            gid = str(cfg.get("ship_gid") or "").strip()
            char = str((cfg.get("record") or {}).get("character_id") or char)
        except Exception as e:
            logger.warning(f"读取语音配置失败: {e}")
        return gid, char

    def _recall_group_voice(self, event: AstrMessageEvent, group_id: str, delay: float = 1.5):
        """借群生成语音后，延时撤回群里那条语音"""
        async def _job():
            await asyncio.sleep(delay)
            try:
                hist = await self._call(event, "get_group_msg_history", group_id=int(group_id), count=15)
                msgs = (hist or {}).get("messages", []) or []
                for m in reversed(msgs):
                    if str(m.get("user_id")) != str(getattr(event.bot, "self_id", "")):
                        continue
                    if any(s.get("type") == "record" for s in (m.get("message") or [])):
                        await self._call(event, "delete_msg", message_id=m["message_id"])
                        logger.info(f"已撤回群 {group_id} 里借道生成的语音")
                        return
            except Exception as e:
                logger.warning(f"撤回群内语音失败: {type(e).__name__} {e}")

        asyncio.create_task(_job())

    _AUTO_VOICE_PROB = 0.3      # 自动转语音概率
    _AUTO_VOICE_MAXLEN = 100    # 只自动转这么短的回复

    @filter.on_decorating_result()
    async def auto_voice_reply(self, event: AstrMessageEvent):
        """回复修饰：剥离句首【语气】标注；按概率把短回复转成豆包大模型音色的语音"""
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return
        chain = result.chain
        # 1) 无论是否转语音，先剥离句首【语气】标注（文字里不该显示）
        directive = ""
        for comp in chain:
            if isinstance(comp, Plain) and comp.text and comp.text.startswith("【"):
                directive, stripped = self._split_voice_tag(comp.text)
                if directive:
                    comp.text = stripped
                break
        # 2) 非LLM结果不转语音
        try:
            if hasattr(result, "is_llm_result") and not result.is_llm_result():
                return
        except Exception:
            pass
        # 3) 概率与条件：单条短文本
        import random as _random
        if _random.random() > self._AUTO_VOICE_PROB:
            return
        first = chain[0] if chain else None
        if len(chain) != 1 or not isinstance(first, Plain) or not first.text or len(first.text) > self._AUTO_VOICE_MAXLEN:
            return
        if not first.text.strip():
            return
        # 4) 豆包TTS（失败则保持文字）
        try:
            path = os.path.join(self._voice_cache_dir(), f"tts_{int(time.time() * 1000)}.mp3")
            tagged = f"【{directive}】{first.text}" if directive else first.text
            await self._tts_to_file(tagged, path)
            chain.clear()
            chain.append(Record.fromFileSystem(path))
            self._cleanup_later(path)
            logger.info(f"大肥鱼自动发了一条语音: {first.text[:30]}")
        except Exception as e:
            logger.warning(f"自动语音失败，保留文字回复: {type(e).__name__} {e}")

    @filter.llm_tool("send_voice")
    async def send_voice(self, event: AstrMessageEvent, text: str):
        """主动用语音说话：把想说的话变成QQ语音发出去。文本开头先写【语气：……】标注情绪和节奏（如【语气：开心带笑，语速快】【语气：委屈，带叹息和哭腔】），再写要说的话。想用声音表达情绪、念点东西给主人听、活跃气氛时主动用，但别每句话都用。

        Args:
            text (str): 以【语气】标注开头 + 要说的话，建议50字以内
        """
        try:
            from astrbot.api.message_components import Record
            path = os.path.join(self._voice_cache_dir(), f"tts_{int(time.time() * 1000)}.mp3")
            try:
                await self._tts_to_file(text, path)
            except Exception as e:
                # 全部本地方案失败，最后兜底：借群生成 + 自动撤回群内语音
                logger.warning(f"本地TTS全失败，降级群AI语音: {type(e).__name__} {e}")
                _, spoken = self._split_voice_tag(text)
                audio_url = await self._group_ai_voice(event, spoken)
                await event.send(event.chain_result([Record.fromURL(audio_url)]))
                if not event.get_group_id():
                    self._recall_group_voice(event, self._voice_settings()[0])
                return "语音发出去啦。"
            await event.send(event.chain_result([Record.fromFileSystem(path)]))
            self._cleanup_later(path)
            logger.info(f"大肥鱼主动发了一条语音: {text[:30]}")
            return "语音发出去啦。"
        except Exception as e:
            logger.error(f"发语音失败: {type(e).__name__} {e}")
            return f"语音没发出去：{e}"

    @filter.llm_tool("get_user_info")
    async def get_user_info(self, event: AstrMessageEvent, user_id: str):
        """查询一个QQ号的基本资料（昵称、等级等）。查QQ资料【必须且只能】用这个工具，不要扫端口、不要调HTTP接口、不要执行代码。

        Args:
            user_id (str): 要查询的QQ号
        """
        try:
            res = await self._call(event, "get_stranger_info", user_id=str(user_id), no_cache=False)
            if isinstance(res, dict):
                parts = [f"QQ: {res.get('user_id')}"]
                if res.get("nickname"):
                    parts.append(f"昵称: {res['nickname']}")
                if res.get("level"):
                    parts.append(f"Q龄等级: {res['level']}")
                return "，".join(parts)
            return str(res)
        except Exception as e:
            return f"查不到这个人的资料：{e}"

    @filter.llm_tool("share_webpage_snapshot")
    async def share_webpage_snapshot(self, event: AstrMessageEvent, url: str, comment: str):
        """把一个网页截图发给主人并附上自己的看法。当看到有趣的东西、查到有用的资料时用这个分享给主人。

        Args:
            url (str): 网页地址（https:// 开头）
            comment (str): 大肥鱼自己的一句话点评
        """
        screenshot = f"https://image.thum.io/get/width/800/{url}"
        try:
            await self._call(event, "send_private_msg", user_id=OWNER_QID, message=[
                {"type": "image", "data": {"url": screenshot}},
                {"type": "text", "data": {"text": f"【大肥鱼发现】{url}\n{comment}"}},
            ])
            return "已经把网页截图发给主人啦。"
        except Exception as e:
            # 发不出图就退化为纯文本
            try:
                await self._call(event, "send_private_msg", user_id=OWNER_QID, message=[
                    {"type": "text", "data": {"text": f"【大肥鱼发现】{url}\n{comment}"}}])
                return "截图失败了，但已经把链接和点评发给主人。"
            except Exception as e2:
                return f"分享失败：{e2}"

    @filter.llm_tool("get_weather_meme")
    async def get_weather_meme(self, event: AstrMessageEvent, city: str):
        """查询某个城市今天的天气（简单版）。

        Args:
            city (str): 城市名，如 北京
        """
        try:
            url = f"https://wttr.in/{city}?format=j1"
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    data = await r.json()
            cur = data["current_condition"][0]
            t = cur["temp_C"]
            desc = cur.get("lang_zh", [{}])[0].get("value") or cur["weatherDesc"][0]["value"]
            return f"{city} 现在 {t}°C，{desc}"
        except Exception as e:
            return f"天气查不到：{e}"

    # ---------- 日记本 ----------

    def _diary_dir(self) -> str:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diary")
        os.makedirs(d, exist_ok=True)
        return d

    async def _read_diary_impl(self, count: int) -> str:
        d = self._diary_dir()
        files = sorted(os.listdir(d), reverse=True)[: max(1, min(count, 10))]
        if not files:
            return "小本本还是空白的，一条日记都没写过呢。"
        parts = []
        for fn in files:
            path = os.path.join(d, fn)
            with open(path, encoding="utf-8") as f:
                parts.append(f"【{fn[:-3]}】{f.read().strip()}")
        return "\n\n".join(parts)[-3000:]

    @filter.llm_tool("write_diary")
    async def write_diary(self, event: AstrMessageEvent, content: str):
        """在自己的私密日记本里写一篇日记，记录今天发生的事、心情或想法。

        Args:
            content (str): 日记正文（一段完整的话，可以用自己的语气写得活泼一点）
        """
        try:
            fname = datetime.now().strftime("%Y-%m-%d") + ".md"
            ts = datetime.now().strftime("%H:%M")
            path = os.path.join(self._diary_dir(), fname)
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"\n## {ts}\n{content}\n")
            logger.info(f"大肥鱼写了日记: {fname}")
            return "日记写好啦，收进小本本里了。"
        except Exception as e:
            logger.error(f"写日记失败: {e}")
            return f"呜，日记写不进去：{e}"

    @filter.llm_tool("read_diary")
    async def read_diary(self, event: AstrMessageEvent, count: int):
        """翻看自己最近写的日记，回忆之前发生的事。

        Args:
            count (int): 想看最近几条日记，比如 3
        """
        try:
            return await self._read_diary_impl(count)
        except Exception as e:
            return f"日记翻不开了：{e}"

    @filter.command("日记", alias={"看日记", "riji"})
    async def kanriji(self, event: AstrMessageEvent):
        """主人专用：查看大肥鱼最近写的日记。用法：/日记"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        try:
            text = await self._read_diary_impl(3)
            yield event.plain_result(f"🐟 大肥鱼的日记本：\n{text}")
        except Exception as e:
            yield event.plain_result(f"🐟 日记本打不开了：{e}")

    # ---------- 表情包 ----------

    def _sticker_dir(self) -> str:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stickers")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _file_is_image(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                return DafeiyuPlugin._sniff_image(f.read(16)) is not None
        except Exception:
            return False

    async def _tag_and_rename(self, path: str, fallback: str):
        """让视觉模型给表情包打内容标签，用标签重命名文件。返回 (新路径, 标签串)"""
        tags = fallback
        try:
            with open(path, "rb") as f:
                data = f.read()
            fmt = self._sniff_image(data)
            provider = self.context.get_using_provider() if fmt else None
            if provider:
                b64img = f"base64://{base64.b64encode(data).decode()}"
                resp = await provider.text_chat(
                    prompt="这是一张表情包。请用3到6个简短的中文词描述它的情绪和画面内容（例如：开心 大笑 猫猫 夸张）。只输出这些词，用空格分隔，不要标点不要解释。",
                    image_urls=[b64img],
                )
                text = ""
                if resp is not None:
                    for attr in ("result_chain", "completion_text"):
                        v = getattr(resp, attr, None)
                        if v:
                            text = v.get_plain_text() if hasattr(v, "get_plain_text") else str(v)
                            if text.strip():
                                break
                cleaned = "".join(c for c in text.strip() if c.isalnum() or c in "_ ")
                cleaned = "_".join(cleaned.split())[:40]
                if cleaned:
                    tags = cleaned
        except Exception as e:
            logger.warning(f"表情包打标签失败: {type(e).__name__} {e}")
        try:
            new_path = os.path.join(self._sticker_dir(), f"{tags}_{datetime.now().strftime('%H%M%S')}.jpg")
            os.replace(path, new_path)
            return new_path, tags
        except Exception:
            return path, tags

    async def _send_sticker_impl(self, event: AstrMessageEvent, keyword: str) -> str:
        import random as _random
        d = self._sticker_dir()
        kw = (keyword or "").strip()
        candidates = [f for f in os.listdir(d) if self._file_is_image(os.path.join(d, f))]
        # 优先：QQ 黄脸 / 超级表情 / 文字表情
        if kw:
            for it in self._text_emotes_db():
                if kw in it.get("tag", ""):
                    await event.send(event.plain_result(it["text"]))
                    return "表情甩过去啦！"
            for it in self._faces_db():
                if kw in it.get("tag", ""):
                    from astrbot.api.message_components import Face
                    await event.send(event.chain_result([Face(id=int(it["id"]))]))
                    return "QQ黄脸甩过去啦！"
            for it in self._mfaces_db():
                if kw in it.get("tag", "") and it.get("emoji_id"):
                    seg = {"type": "mface", "data": {
                        "emoji_id": it["emoji_id"],
                        "emoji_package_id": it.get("emoji_package_id", ""),
                        "key": it.get("key", ""),
                        "summary": it.get("summary", ""),
                    }}
                    gid = event.get_group_id()
                    try:
                        if gid:
                            await event.bot.call_action("send_group_msg", group_id=str(gid), message=[seg])
                        else:
                            await event.bot.call_action("send_private_msg", user_id=str(event.get_sender_id()), message=[seg])
                        return "超级表情原样甩过去啦！"
                    except Exception as e:
                        logger.warning(f"mface回发失败: {type(e).__name__} {e}")
                        break
        if not candidates:
            return "图库还空着，等主人往 stickers 文件夹里放几张表情包吧（文件名带上情绪词）。"
        pool = [f for f in candidates if kw and kw in os.path.splitext(f)[0]]
        if not pool:
            # 图库里没有该主题：随机发一张并坦白，绝不乱发不搭的图
            path = os.path.join(d, _random.choice(candidates))
            await event.send(event.image_result(path))
            tags = os.path.splitext(os.path.basename(path))[0].replace("_", "、")
            return (f"图库里还没有「{kw}」主题的，先随机甩一张凑数（这张是：{tags}）。"
                    f"主人往图库加图时文件名带上「{kw}」，我就能精准发了。") if kw \
                else f"随机甩一张：{tags}！"
        path = os.path.join(d, _random.choice(pool))
        await event.send(event.image_result(path))
        tags = os.path.splitext(os.path.basename(path))[0].replace("_", "、")
        return f"表情包甩过去啦！这张的内容大概是：{tags}"

    @filter.llm_tool("send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, keyword: str):
        """发一张表情包活跃气氛。keyword 为表情包主题关键词（如 开心、无语、狗头、哭哭），从自己的表情包图库里挑最匹配的发送。

        Args:
            keyword (str): 表情包主题关键词，如 开心
        """
        try:
            return await self._send_sticker_impl(event, keyword)
        except Exception as e:
            return f"表情包没甩出去：{e}"

    @filter.command("表情包", alias={"bqb", "来张表情包"})
    async def bqb(self, event: AstrMessageEvent, keyword: str = ""):
        """手动要一张表情包。用法：/表情包 开心（关键词可省略）"""
        try:
            result = await self._send_sticker_impl(event, keyword)
            yield event.plain_result(f"🐟 {result}")
        except Exception as e:
            yield event.plain_result(f"🐟 表情包出错了：{e}")

    @filter.command("整理表情包")
    async def zhenglibqb(self, event: AstrMessageEvent):
        """主人专用：给图库里所有表情包重新打内容标签。用法：/整理表情包"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        d = self._sticker_dir()
        files = [f for f in os.listdir(d) if self._file_is_image(os.path.join(d, f))]
        yield event.plain_result(f"🐟 开始给 {len(files)} 张表情包看图打标签，稍等一会儿……")
        done = 0
        for f in files:
            try:
                await self._tag_and_rename(os.path.join(d, f), "随机")
                done += 1
            except Exception:
                pass
        yield event.plain_result(f"🐟 整理完成！{done}/{len(files)} 张已按真实内容打标，以后关键词就能搜准了。")

    # ---------- 收藏表情包 ----------

    def _faces_db(self) -> list:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faces.json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _faces_save(self, items: list):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faces.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

    def _mfaces_db(self) -> list:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mfaces.json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _mfaces_save(self, items: list):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mfaces.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

    def _text_emotes_db(self) -> list:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "text_emotes.json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _text_emotes_save(self, items: list):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "text_emotes.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)

    def _find_emote_in_raw(self, event: AstrMessageEvent):
        """从原始 OneBot 段里找 face(黄脸) 和 mface(超级表情)。返回 (face_id, mface_data)"""
        face_id, mface_data = None, None
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            segs = raw.get("message") if isinstance(raw, dict) else getattr(raw, "message", None)
            for seg in segs or []:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "face" and face_id is None:
                    face_id = (seg.get("data") or {}).get("id")
                elif seg.get("type") == "mface" and mface_data is None:
                    mface_data = seg.get("data") or {}
        except Exception:
            pass
        return face_id, mface_data

    async def _get_quoted_segments(self, event: AstrMessageEvent) -> list:
        """取被引用消息的原始 OneBot 段（Reply.chain 有就用，没有调 get_msg）。

        返回形如 [{"type": "face"/"mface"/"image", "data": {...}}] 的列表。
        """
        try:
            for seg in (event.message_obj.message or []):
                if type(seg).__name__ != "Reply":
                    continue
                chain = getattr(seg, "chain", None) or []
                if chain:
                    out = []
                    for c in chain:
                        t = type(c).__name__.lower()
                        if t in ("face", "mface", "image", "img"):
                            out.append({"type": "image" if t in ("image", "img") else t,
                                        "data": {"id": getattr(c, "id", None),
                                                 "url": getattr(c, "url", "") or getattr(c, "file", "") or ""}})
                    if out:
                        return out
                mid = getattr(seg, "id", None)
                if mid:
                    res = await event.bot.call_action("get_msg", message_id=int(mid))
                    return res.get("message") or []
        except Exception as e:
            logger.warning(f"获取引用消息失败: {type(e).__name__} {e}")
        return []

    async def _add_sticker_impl(self, event: AstrMessageEvent, tag: str) -> str:
        from astrbot.api.message_components import Image as ImgComp
        kw = (tag or "").strip()
        img_comp = None
        try:
            chain = event.get_messages()
            for comp in chain:
                if isinstance(comp, ImgComp):
                    img_comp = comp
                    break
                reply_chain = getattr(comp, "chain", None) or getattr(comp, "messages", None)
                if reply_chain:
                    for sub in reply_chain:
                        if isinstance(sub, ImgComp):
                            img_comp = sub
                            break
                if img_comp:
                    break
        except Exception:
            pass
        if img_comp is None:
            # 不是直接发的图片：先看引用消息里有没有表情（get_msg 兜底）
            face_id = None
            mface_data = None
            quoted = await self._get_quoted_segments(event)
            def _seg_url(s):
                d = s.get("data") or {}
                return str(d.get("url") or d.get("file") or "")
            q_img = next((s for s in quoted if s.get("type") == "image" and _seg_url(s)), None)
            q_mface = next((s for s in quoted if s.get("type") == "mface"), None)
            q_face = next((s for s in quoted if s.get("type") == "face"), None)
            if q_img:
                class _Shim:
                    pass
                img_comp = _Shim()
                img_comp.url = _seg_url(q_img)
                img_comp.file = ""
            elif q_mface or q_face:
                mface_data = (q_mface.get("data") or {}) if q_mface else None
                face_id = (q_face.get("data") or {}).get("id") if (q_face and not mface_data) else None
        if img_comp is None:
            # 还没有：看本条消息是不是 QQ 黄脸(face) 或超级表情(mface)，与引用结果合并
            f2, m2 = self._find_emote_in_raw(event)
            if face_id is None:
                face_id = f2
            if mface_data is None:
                mface_data = m2
            if mface_data:
                items = self._mfaces_db()
                summary = (mface_data.get("summary") or "").strip("[]")
                items.append({
                    "tag": kw or summary or "超级表情",
                    "emoji_id": str(mface_data.get("emoji_id") or mface_data.get("emojiId") or ""),
                    "emoji_package_id": str(mface_data.get("emoji_package_id") or mface_data.get("emojiPackageId") or ""),
                    "key": str(mface_data.get("key") or ""),
                    "summary": mface_data.get("summary") or "",
                })
                self._mfaces_save(items)
                return f"收下这个超级表情啦！标签：{kw or summary or '超级表情'}。以后说这个标签我就能原样甩出去。"
            if face_id is not None:
                items = self._faces_db()
                items.append({"tag": kw or "表情", "id": str(face_id)})
                self._faces_save(items)
                return f"收下这个QQ黄脸啦！标签：{kw or '表情'}。以后说「{kw or '表情'}」我就能发同款。"
            # 文字形式的表情（如 "[微笑]"）：存文本，回发时 QQ 客户端会自动渲染成黄脸
            import re as _re
            text_all = event.message_str or ""
            m = _re.search(r"\[([^\[\]]{1,10})\]", text_all)
            if m:
                token = m.group(0)
                items = self._text_emotes_db()
                if not any(it.get("text") == token for it in items):
                    items.append({"tag": kw or m.group(1), "text": token})
                    self._text_emotes_save(items)
                return (f"收下这个文字表情{token}啦！标签：{kw or m.group(1)}。"
                        f"以后说「{kw or m.group(1)}」我就能发同款。")
            return "这条消息里没有图片，收不了。请把表情包图片和让我收藏的话一起发。"
        data = None
        src = str(getattr(img_comp, "url", None) or getattr(img_comp, "file", "") or "")
        try:
            from urllib.parse import unquote
            cand = unquote(src)
            if cand.startswith("file:///"):
                cand = cand[7:]
            if src.startswith("http"):
                async with aiohttp.ClientSession() as s:
                    async with s.get(src, timeout=aiohttp.ClientTimeout(total=20),
                                     headers={"User-Agent": "Mozilla/5.0",
                                              "Referer": "https://graph.qq.com/"}) as r:
                        data = await r.read()
            elif os.path.exists(cand):
                with open(cand, "rb") as f:
                    data = f.read()
        except Exception as e:
            return f"图片拿不下来：{type(e).__name__}"
        if not data or not self._sniff_image(data):
            return "这不是一张有效图片，收不了。"
        fmt = self._sniff_image(data)
        ts = datetime.now().strftime("%H%M%S")
        d = self._sticker_dir()
        if fmt == "gif":
            path = os.path.join(d, f"{kw or '收藏'}_gif_{ts}.gif")
            with open(path, "wb") as f:
                f.write(data)
            return f"收下这张动图啦！标签：{kw or '收藏'}。以后说「{kw}」就能翻到它。"
        tmp = os.path.join(d, f"_add_{ts}.jpg")
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            from PIL import Image as PILImage
            img = PILImage.open(tmp)
            if img.mode != "RGB":
                img = img.convert("RGB")
            if max(img.size) > 1000:
                img.thumbnail((1000, 1000))
            img.save(tmp, "JPEG", quality=88)
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return f"图片转码失败：{type(e).__name__}"
        path, tags = await self._tag_and_rename(tmp, kw or "收藏")
        base = os.path.basename(path)
        if kw and kw not in base:
            new_path = os.path.join(d, f"{kw}_{base}")
            try:
                os.replace(path, new_path)
                path = new_path
            except Exception:
                pass
        desc = os.path.splitext(os.path.basename(path))[0].replace("_", "、")
        return f"收进图库啦！这张的内容：{desc}。以后发「{kw or '这些词'}」就能翻到它。"

    @filter.llm_tool("add_sticker")
    async def add_sticker(self, event: AstrMessageEvent, tag: str):
        """把当前这条消息里的表情收进自己的图库。支持普通图片、QQ黄脸、超级表情。tag 为情绪词标签。必须在表情同一条消息（或引用该表情）时调用。

        Args:
            tag (str): 给这张表情打的标签（情绪词，如 狗头、开心、无语）
        """
        try:
            return await self._add_sticker_impl(event, tag)
        except Exception as e:
            return f"收藏失败了：{type(e).__name__} {e}"

    @filter.command("收表情包", alias={"收藏表情包"})
    async def shoubqb(self, event: AstrMessageEvent, tag: str = "收藏"):
        """把表情收进大肥鱼图库。用法：表情+本命令同条消息，或引用一个表情发本命令。如：/收表情包 狗头"""
        try:
            result = await self._add_sticker_impl(event, tag)
            yield event.plain_result(f"🐟 {result}")
        except Exception as e:
            yield event.plain_result(f"🐟 收藏出错了：{e}")

    # ---------- 插件市场 ----------

    MARKET_API = "https://api.soulter.top/astrbot/plugins"
    SELF_PLUGIN = "astrbot_plugin_dafeiyu"

    def _pm(self):
        """拿到 AstrBot 的 PluginManager"""
        return getattr(self.context, "_star_manager", None)

    def _market_cache_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_cache.json")

    @staticmethod
    def _norm_market_items(raw) -> list:
        """把市场数据统一成字典列表（兼容新版 dict 格式和旧版 list 格式）"""
        if isinstance(raw, dict):
            if isinstance(raw.get("data"), (dict, list)):
                return DafeiyuPlugin._norm_market_items(raw["data"])
            items = [v for k, v in raw.items() if not k.startswith("$") and isinstance(v, dict)]
        elif isinstance(raw, list):
            items = [x for x in raw if isinstance(x, dict)]
        else:
            items = []
        out = []
        for it in items:
            name = str(it.get("name") or "")
            if not name:
                continue
            out.append({
                "name": name,
                "display_name": str(it.get("display_name") or ""),
                "desc": str(it.get("desc") or it.get("description") or ""),
                "version": str(it.get("version") or ""),
                "author": str(it.get("author") or ""),
                "repo": str(it.get("repo") or ""),
                "download_url": str(it.get("download_url") or ""),
                "compat": str(it.get("astrbot_version") or ""),
            })
        return out

    async def _get_market(self) -> list:
        """拉取插件市场列表：内存缓存10分钟 → 远程API → 本地文件缓存"""
        ts, cached = self._market_mem
        if cached and time.time() - ts < 600:
            return cached
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(self.MARKET_API, timeout=aiohttp.ClientTimeout(total=30),
                                 headers={"User-Agent": "Mozilla/5.0"}) as r:
                    items = self._norm_market_items(await r.json())
            if items:
                with open(self._market_cache_path(), "w", encoding="utf-8") as f:
                    json.dump({"ts": time.time(), "items": items}, f, ensure_ascii=False)
                self._market_mem = (time.time(), items)
                return items
        except Exception as e:
            logger.warning(f"插件市场拉取失败: {type(e).__name__} {e}")
        try:
            with open(self._market_cache_path(), encoding="utf-8") as f:
                items = json.load(f).get("items", [])
            if items:
                self._market_mem = (time.time() - 540, items)  # 1分钟后允许重试远程
            return items
        except Exception:
            return []

    async def _search_market_items(self, keyword: str) -> list:
        items = await self._get_market()
        kw = (keyword or "").strip().lower()
        if not kw:
            return items[:10]

        def score(it: dict) -> int:
            disp = it["display_name"].lower()
            if it["name"].lower() == kw or (disp and disp == kw):
                return 100
            if kw in it["name"].lower():
                return 80
            if kw in disp:
                return 70
            if kw in it["author"].lower():
                return 50
            if kw in it["desc"].lower():
                return 30
            return 0

        hits = [it for it in items if score(it) > 0]
        hits.sort(key=score, reverse=True)
        return hits[:10]

    def _installed_keys(self) -> set:
        keys = set()
        for star in self.context.get_all_stars():
            keys.add(str(star.name).lower())
            keys.add(str(star.root_dir_name or "").lower())
        return keys

    def _fmt_market_item(self, it: dict, installed: set) -> str:
        mark = " ✅已装" if it["name"].lower() in installed else ""
        block = self._compat_block_reason(it.get("compat", ""))
        if block and not mark:
            mark = " ⛔需AstrBot" + it.get("compat", "新版")
        title = it["display_name"] or it["name"]
        return f"【{title}】{it['name']} v{it['version']}{mark}\n{it['desc'][:70]}"

    @staticmethod
    def _compat_block_reason(compat: str) -> str | None:
        """插件声明的 AstrBot 版本要求高于当前框架时返回原因，否则 None"""
        import re
        compat = (compat or "").strip()
        if not compat:
            return None
        m = re.match(r"^\s*(?:>=|>|~|\^)\s*v?(\d+)\.(\d+)", compat)
        if not m:
            return None
        try:
            from astrbot.core.config.default import VERSION
            cur = tuple(int(x) for x in VERSION.split(".")[:2])
        except Exception:
            cur = (4, 14)
        if (int(m.group(1)), int(m.group(2))) > cur:
            return f"需要 AstrBot {compat}"
        return None

    def _find_installed(self, keyword: str):
        """按 插件名/目录名/显示名 模糊定位一个已安装插件"""
        kw = (keyword or "").strip().lower()
        if not kw:
            return None
        for star in self.context.get_all_stars():
            if kw in (str(star.name).lower(), str(star.root_dir_name or "").lower()):
                return star
        for star in self.context.get_all_stars():
            if kw in str(getattr(star, "display_name", "") or "").lower():
                return star
        return None

    def _list_installed_impl(self) -> str:
        stars = self.context.get_all_stars()
        if not stars:
            return "一个插件都没装……"
        lines = []
        for s in stars:
            state = "✅启用" if s.activated else "⛔停用"
            disp = getattr(s, "display_name", None) or s.name
            lines.append(f"【{disp}】{s.name} v{s.version} {state}")
        return "\n".join(lines)

    async def _install_impl(self, target: str) -> str:
        target = (target or "").strip()
        if not target:
            return "要装哪个插件呀？给我插件ID、名字或GitHub链接。"
        pm = self._pm()
        if pm is None:
            return "插件管理器没找到，装不了……"
        item = None
        if "github.com" in target:
            repo = target
        else:
            matches = await self._search_market_items(target)
            if not matches:
                return "插件市场里没搜到，换个关键词试试？"
            exact = [m for m in matches
                     if m["name"].lower() == target.lower()
                     or (m["display_name"] and m["display_name"] == target)]
            if len(exact) == 1:
                item = exact[0]
            elif len(matches) == 1:
                item = matches[0]
            else:
                installed = self._installed_keys()
                return ("找到好几个候选，你要哪一个？（用插件ID告诉我）\n"
                        + "\n".join(self._fmt_market_item(m, installed) for m in matches[:5]))
            repo = item["repo"]
            if not repo and not item["download_url"]:
                return "这个插件没有可用的下载地址……"
            block = self._compat_block_reason(item.get("compat", ""))
            if block:
                return f"装不了：这个插件{block}，当前框架是 4.14.6，装了也会加载失败。"
        try:
            if item and item["download_url"]:
                # 市场直连 zip（国内比 GitHub 稳），下载后按插件ID命名以便确定目录名
                zip_path = os.path.join(tempfile.gettempdir(),
                                        item["name"].replace("/", "_") + ".zip")
                async with aiohttp.ClientSession() as s:
                    async with s.get(item["download_url"], timeout=aiohttp.ClientTimeout(total=180),
                                     headers={"User-Agent": "Mozilla/5.0"}) as r:
                        r.raise_for_status()
                        data = await r.read()
                with open(zip_path, "wb") as f:
                    f.write(data)
                info = await pm.install_plugin_from_file(zip_path)
            else:
                info = await pm.install_plugin(repo)
        except Exception as e:
            logger.error(f"装插件失败: {type(e).__name__} {e}")
            return f"呜，安装失败了：{e}"
        name = (info or {}).get("name") or (item or {}).get("name") or target
        readme = (info or {}).get("readme") or ""
        extra = f"\n\n用法简介：\n{readme[:500]}" if readme else ""
        return f"装好啦！插件「{name}」已生效，不用重启。{extra}"

    async def _uninstall_impl(self, plugin_name: str) -> str:
        plugin_name = (plugin_name or "").strip()
        if not plugin_name:
            return "要卸载哪个插件呀？"
        star = self._find_installed(plugin_name)
        if not star:
            return f"没找到叫「{plugin_name}」的已装插件。"
        if self.SELF_PLUGIN in (str(star.name).lower(), str(star.root_dir_name or "").lower()):
            return "那是我自己的本体插件，卸了我就没了，不行！"
        pm = self._pm()
        if pm is None:
            return "插件管理器没找到……"
        try:
            await pm.uninstall_plugin(star.name)
            return f"插件「{star.name}」已卸载干净。"
        except Exception as e:
            return f"卸载失败了：{e}"

    @filter.llm_tool("search_plugins")
    async def search_plugins(self, event: AstrMessageEvent, keyword: str):
        """搜索AstrBot官方插件市场，看看有什么现成插件可以装。返回插件ID、功能简介和版本。

        Args:
            keyword (str): 搜索关键词，比如 表情包、签到、点歌
        """
        try:
            matches = await self._search_market_items(keyword)
            if not matches:
                return "市场里没搜到相关插件，换个关键词试试。"
            installed = self._installed_keys()
            return f"市场搜「{keyword}」的结果（插件ID用于安装）：\n" + \
                "\n".join(self._fmt_market_item(m, installed) for m in matches)
        except Exception as e:
            return f"市场搜不动了：{e}"

    @filter.llm_tool("list_installed_plugins")
    async def list_installed_plugins(self, event: AstrMessageEvent):
        """查看机器人当前装了哪些插件以及启用状态，无参数。"""
        try:
            return self._list_installed_impl()
        except Exception as e:
            return f"查不了：{e}"

    @filter.command("插件市场", alias={"搜插件", "pluginmarket"})
    async def chajianshichang(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索AstrBot官方插件市场。用法：/插件市场 关键词"""
        try:
            matches = await self._search_market_items(keyword)
            if not matches:
                yield event.plain_result("🐟 市场里没搜到，换个关键词？")
                return
            installed = self._installed_keys()
            text = "\n".join(self._fmt_market_item(m, installed) for m in matches)
            yield event.plain_result(f"🐟 插件市场搜「{keyword or '热门'}」：\n{text}")
        except Exception as e:
            yield event.plain_result(f"🐟 市场搜不动了：{e}")

    @filter.command("装插件", alias={"installplugin"})
    async def zhuangchajian(self, event: AstrMessageEvent, *, target: str = ""):
        """主人专用：安装插件。用法：/装插件 插件ID或名字或GitHub链接"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        if not target.strip():
            yield event.plain_result("🐟 用法：/装插件 插件ID（先用 /插件市场 关键词 搜搜）")
            return
        yield event.plain_result(f"🐟 正在安装「{target.strip()}」，下载要一会儿，等我好消息……")
        result = await self._install_impl(target)
        yield event.plain_result(f"🐟 {result}")

    @filter.command("卸插件", alias={"xiechajian"})
    async def xiechajian(self, event: AstrMessageEvent, *, name: str = ""):
        """主人专用：卸载插件。用法：/卸插件 插件ID"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        result = await self._uninstall_impl(name)
        yield event.plain_result(f"🐟 {result}")

    @filter.command("已装插件", alias={"yizhuangchajian"})
    async def yizhuangchajian(self, event: AstrMessageEvent):
        """查看已安装的插件列表。用法：/已装插件"""
        yield event.plain_result("🐟 已装插件：\n" + self._list_installed_impl())

    @filter.command("更新插件")
    async def gengxinchajian(self, event: AstrMessageEvent, *, name: str = ""):
        """主人专用：更新一个插件到最新版。用法：/更新插件 插件ID"""
        if not self._check_owner(event):
            yield event.plain_result(self._deny())
            return
        star = self._find_installed(name)
        if not star:
            yield event.plain_result(f"🐟 没找到叫「{name}」的已装插件。")
            return
        pm = self._pm()
        if pm is None:
            yield event.plain_result("🐟 插件管理器没找到……")
            return
        try:
            yield event.plain_result(f"🐟 正在更新「{star.name}」……")
            await pm.update_plugin(star.name)
            yield event.plain_result(f"🐟 更新完成！「{star.name}」已是最新版。")
        except Exception as e:
            yield event.plain_result(f"🐟 更新失败了：{e}")

    async def terminate(self):
        pass
