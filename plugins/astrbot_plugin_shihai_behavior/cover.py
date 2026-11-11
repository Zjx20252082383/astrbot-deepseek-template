"""AstrBot front end for the RVC AI-cover service."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain, Record, Reply
from astrbot.api.star import Context, Star, register
from quart import request as web_request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

DEFAULT_FORM_VALUES = {
    "index_rate": 0.75,
    "rms_mix_rate": 0.25,
    "protect": 0.25,
}
DEFAULT_VOCAL_GAIN = 1.0
DEFAULT_INSTRUMENTAL_GAIN = 1.0
DEFAULT_MODEL = "芙宁娜"
MAX_GAIN = 2.0
MIN_SPEED = 0.5
MAX_SPEED = 2.0
PLUGIN_NAME = "astrbot_plugin_ai_cover"
INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def _safe_filename_part(value: str, fallback: str) -> str:
    """Return a platform-safe value for one part of an attachment filename."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", str(value or "")).strip(" .")
    return cleaned or fallback


def _cover_filename(model: str, original_name: str) -> str:
    """Build an MP3 attachment name from the role and source filename."""
    source_name = str(original_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    source_stem = Path(source_name).stem
    role = _safe_filename_part(model, "未知角色")
    source = _safe_filename_part(source_stem, "源音频")
    return f"AI翻唱-{role}-{source}.mp3"


class OriginalFormatRecord(Record):
    """A Record that preserves the source audio instead of converting it to WAV."""

    @staticmethod
    def fromFileSystem(path: str | Path, **kwargs) -> OriginalFormatRecord:
        file_path = Path(path).resolve(strict=False)
        return OriginalFormatRecord(
            file=file_path.as_uri(),
            path=str(file_path),
            **kwargs,
        )

    async def convert_to_base64(self) -> str:
        if not self.path:
            return await super().convert_to_base64()

        def encode_file() -> str:
            return base64.b64encode(Path(self.path).read_bytes()).decode("ascii")

        return await asyncio.to_thread(encode_file)


class CoverMixin:
    """RVC 翻唱模块（原 astrbot_plugin_ai_cover 整体迁入，无 @register）。"""

    def setup_cover(self, config):
        self.config = config
        self.service_url = str(
            config.get("service_url", "http://127.0.0.1:18888")
        ).rstrip("/")
        self.api_token = str(config.get("api_token", ""))
        self.timeout_seconds = max(60, int(config.get("timeout_seconds", 3600)))
        self.data_dir = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_ai_cover"
        self.output_dir = self.data_dir / "outputs"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir = self.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # 最近收到的音频（时间戳, 路径, 文件名）：支持"先发音频再说翻唱"的跨消息记忆
        self._recent_audio: list[tuple[float, str, str]] = []
        # 防重复：同一音源 15 分钟内只发一次（发送成功才记录）
        self._last_cover_key: str | None = None
        self._last_cover_at: float = 0.0
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache",
            self.page_cache_status,
            ["GET"],
            "AI cover separation cache status",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/clear",
            self.page_clear_cache,
            ["POST"],
            "Clear AI cover separation cache",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/cache/delete",
            self.page_delete_cache,
            ["POST"],
            "Delete selected AI cover separation cache entries",
        )

    def _headers(self) -> dict[str, str]:
        return {"X-AI-Cover-Token": self.api_token} if self.api_token else {}

    async def _service_json(
        self,
        path: str,
        method: str = "GET",
        json_body: dict | None = None,
    ) -> dict:
        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
            session.request(
                method,
                f"{self.service_url}{path}",
                headers=self._headers(),
                json=json_body,
            ) as response,
        ):
            payload = await response.text()
            if response.status != 200:
                raise RuntimeError(self._error_detail(payload, response.status))
            return json.loads(payload)

    async def _get_json(self, path: str) -> dict:
        return await self._service_json(path)

    async def page_cache_status(self):
        """Expose service cache entries to the plugin management page."""
        try:
            return await self._service_json("/cache")
        except Exception as error:  # noqa: BLE001 - Web API boundary
            return {"status": "error", "message": str(error)}, 502

    async def page_clear_cache(self):
        """Clear the service-side cache from the plugin management page."""
        try:
            payload = await self._service_json("/cache", "DELETE")
            return {
                "removed": payload.get("removed", {}),
                "cache": payload.get("cache", {}),
            }
        except Exception as error:  # noqa: BLE001 - Web API boundary
            return {"status": "error", "message": str(error)}, 502

    async def page_delete_cache(self):
        """Delete selected service-side cache entries from the management page."""
        try:
            body = await web_request.json(default={})
            ids = body.get("ids") if isinstance(body, dict) else None
            if not isinstance(ids, list) or not ids:
                return {"status": "error", "message": "请选择要清理的缓存。"}, 400
            payload = await self._service_json(
                "/cache/delete",
                "POST",
                {"ids": ids},
            )
            return {
                "removed": payload.get("removed", {}),
                "cache": payload.get("cache", {}),
                "items": payload.get("items", []),
            }
        except Exception as error:  # noqa: BLE001 - Web API boundary
            return {"status": "error", "message": str(error)}, 502

    @staticmethod
    def _error_detail(payload: str, status: int) -> str:
        try:
            detail = json.loads(payload).get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = payload.strip()
        return f"RVC 服务返回 HTTP {status}: {detail or '未知错误'}"

    @staticmethod
    def _reply_components(component: object) -> list[object]:
        if isinstance(component, Reply) and component.chain:
            return list(component.chain)
        return []

    async def _find_audio(self, event: AstrMessageEvent) -> tuple[str, str]:
        components = list(event.get_messages())
        for component in list(components):
            components.extend(self._reply_components(component))

        for component in components:
            if isinstance(component, Record):
                path = await component.convert_to_file_path()
                return path, Path(path).name or "record.wav"
            if isinstance(component, File):
                path = await component.get_file()
                if path:
                    return path, component.name or Path(path).name
        # 回退：用户可能先单独发了音频、再发"翻唱"，取最近 1 小时内的音频
        now = time.time()
        for ts, path, name in reversed(self._recent_audio):
            if now - ts <= 3600 and Path(path).exists():
                return path, name
        raise ValueError("请在同一条消息中附带音频文件，或先发一条音频再说翻唱。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_audio(self, event: AstrMessageEvent):
        """记录所有消息里的音频组件，供后续"翻唱"跨消息取用。"""
        try:
            for component in event.get_messages():
                if isinstance(component, Record):
                    path = await component.convert_to_file_path()
                    if path:
                        self._recent_audio.append(
                            (time.time(), path, Path(path).name or "record.wav")
                        )
                elif isinstance(component, File):
                    path = await component.get_file()
                    if path:
                        self._recent_audio.append(
                            (
                                time.time(),
                                path,
                                component.name or Path(path).name,
                            )
                        )
            deadline = time.time() - 3600
            self._recent_audio = [
                item for item in self._recent_audio if item[0] >= deadline
            ][-10:]
        except Exception:  # noqa: BLE001 - listener must never break the pipeline
            pass

    def _cleanup_outputs_sync(self) -> None:
        retention = max(1, int(self.config.get("output_retention_hours", 24)))
        deadline = time.time() - retention * 3600
        for path in self.output_dir.glob("ai_cover_*.mp3"):
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                continue

    def _configured_gain(self, key: str, default: float) -> float:
        """Read a gain safely so configs created before v1.1 still work."""
        try:
            gain = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return gain if math.isfinite(gain) and 0.0 <= gain <= MAX_GAIN else default

    def _configured_model(self) -> str:
        """Return a non-empty default model for commands that omit it."""
        configured = self.config.get("default_model", DEFAULT_MODEL)
        model = str(configured or "").strip()
        return model or DEFAULT_MODEL

    def _resolve_gains(
        self,
        vocal_gain: float,
        instrumental_gain: float,
    ) -> tuple[float, float]:
        gains = {
            "人声音量": (
                vocal_gain,
                self._configured_gain("vocal_gain", DEFAULT_VOCAL_GAIN),
            ),
            "伴奏音量": (
                instrumental_gain,
                self._configured_gain("instrumental_gain", DEFAULT_INSTRUMENTAL_GAIN),
            ),
        }
        resolved: list[float] = []
        for label, (requested, configured) in gains.items():
            gain = configured if requested == -1 else requested
            if not math.isfinite(gain) or not 0.0 <= gain <= MAX_GAIN:
                raise ValueError(f"{label}必须在 0 到 {MAX_GAIN:g} 之间。")
            resolved.append(gain)
        return resolved[0], resolved[1]

    async def _request_cover(
        self,
        audio_path: str,
        original_name: str,
        model: str,
        vocal_transpose: int,
        instrumental_transpose: int,
        vocal_gain: float,
        instrumental_gain: float,
        speed: float,
    ) -> tuple[Path, str, str, bool]:
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=30,
            sock_read=self.timeout_seconds,
        )
        form = aiohttp.FormData()
        handle = await asyncio.to_thread(Path(audio_path).open, "rb")
        form.add_field(
            "audio",
            handle,
            filename=original_name,
            content_type="application/octet-stream",
        )
        form.add_field("model", model)
        form.add_field("transpose", str(vocal_transpose))
        form.add_field("instrumental_transpose", str(instrumental_transpose))
        for key, default in DEFAULT_FORM_VALUES.items():
            form.add_field(key, str(self.config.get(key, default)))
        form.add_field("vocal_gain", str(vocal_gain))
        form.add_field("instrumental_gain", str(instrumental_gain))
        form.add_field("speed", str(speed))

        output = self.output_dir / f"ai_cover_{uuid.uuid4().hex}.mp3"
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
                session.post(
                    f"{self.service_url}/cover",
                    data=form,
                    headers=self._headers(),
                ) as response,
            ):
                if response.status != 200:
                    payload = await response.text()
                    raise RuntimeError(self._error_detail(payload, response.status))
                applied_instrumental_transpose = response.headers.get(
                    "X-AI-Cover-Instrumental-Transpose"
                )
                if (
                    instrumental_transpose != 0
                    and applied_instrumental_transpose
                    != str(instrumental_transpose)
                ):
                    raise RuntimeError(
                        "当前 RVC 服务不支持 BGM 变调，请先更新并重启后端服务。"
                    )
                applied_speed = response.headers.get("X-AI-Cover-Speed")
                try:
                    speed_supported = math.isclose(
                        float(applied_speed), speed, rel_tol=0.0, abs_tol=1e-9
                    )
                except (TypeError, ValueError):
                    speed_supported = False
                if speed != 1.0 and not speed_supported:
                    raise RuntimeError(
                        "当前 RVC 服务不支持变速，请先更新并重启后端服务。"
                    )
                target = await asyncio.to_thread(output.open, "wb")
                try:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        await asyncio.to_thread(target.write, chunk)
                finally:
                    await asyncio.to_thread(target.close)
                if output.stat().st_size == 0:
                    raise RuntimeError("RVC 服务返回了空音频")
                actual_model = unquote(response.headers.get("X-AI-Cover-Model", model))
                index = unquote(response.headers.get("X-AI-Cover-Index", ""))
                cache_hit = response.headers.get("X-AI-Cover-Cache") == "hit"
                return output, actual_model, index, cache_hit
        except Exception:
            output.unlink(missing_ok=True)
            raise
        finally:
            await asyncio.to_thread(handle.close)

    @filter.command("翻唱模型")
    async def cover_models(self, event: AstrMessageEvent):
        """列出当前 RVC 服务可用的翻唱模型。"""
        try:
            payload = await self._get_json("/models")
            rows = payload.get("models", [])
            if not rows:
                yield event.plain_result("RVC 服务中没有发现可用的 .pth 模型。")
                return
            lines = ["可用翻唱模型："]
            for row in rows:
                marker = "✓索引" if row.get("has_index") else "无索引"
                lines.append(f"- {row['name']}（{marker}）")
            lines.append(
                f"\n默认模型：{self._configured_model()}\n"
                "用法：/翻唱 [模型名] [人声变调] [BGM变调] "
                "[人声音量] [背景音乐音量] [变速]，"
                "并附带或回复音频。"
            )
            yield event.plain_result("\n".join(lines))
        except Exception as error:  # noqa: BLE001 - command boundary reports failures
            yield event.plain_result(f"获取翻唱模型失败：{error}")

    async def _run_cover(
        self,
        event: AstrMessageEvent,
        model: str,
        vocal_transpose: int,
        instrumental_transpose: int,
        vocal_gain: float,
        instrumental_gain: float,
        speed: float,
        force_file: bool,
    ):
        """Run one cover job and optionally force MP3 file delivery."""
        model = model.strip() or self._configured_model()
        if not -24 <= vocal_transpose <= 24:
            yield event.plain_result("人声变调必须在 -24 到 24 之间。")
            return
        if not -24 <= instrumental_transpose <= 24:
            yield event.plain_result("BGM 变调必须在 -24 到 24 之间。")
            return
        if not math.isfinite(speed) or not MIN_SPEED <= speed <= MAX_SPEED:
            yield event.plain_result(
                f"变速必须在 {MIN_SPEED:g} 到 {MAX_SPEED:g} 之间，1 为原速。"
            )
            return
        try:
            vocal_gain, instrumental_gain = self._resolve_gains(
                vocal_gain,
                instrumental_gain,
            )
        except ValueError as error:
            yield event.plain_result(str(error))
            return
        try:
            audio_path, original_name = await self._find_audio(event)
        except Exception as error:  # noqa: BLE001 - adapters may raise platform errors
            yield event.plain_result(str(error))
            return
        try:
            st = os.stat(audio_path)
            source_key = f"{model}|{Path(original_name).name}|{st.st_size}"
        except OSError:
            source_key = None
        # 只发语音/文件，不发过程文字
        ok, message = await self._make_and_send_cover(
            event,
            audio_path,
            original_name,
            model,
            vocal_transpose,
            instrumental_transpose,
            speed,
            source_key,
        )
        if not ok:
            yield event.plain_result(message)

    async def _netease_search(self, keyword: str) -> list[dict]:
        """网易云歌曲搜索，返回全部候选（含 id/name/artists）。"""
        url = "https://music.163.com/api/search/get"
        headers = {
            "Referer": "https://music.163.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Cookie": "appver=2.9.7",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data={"s": keyword, "type": 1, "limit": 5, "offset": 0},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    data = await response.json(content_type=None)
        except Exception as error:  # noqa: BLE001 - network boundary
            logger.warning(f"[ai_cover] 网易云搜索失败: {error}")
            return None
        songs = (data.get("result") or {}).get("songs") or []
        return songs[:5]

    async def _netease_download(self, song_id, song_name: str) -> tuple[str, str] | None:
        """通过网易云外链下载歌曲，返回 (本地路径, 文件名)。无版权/试听片段返回 None。"""
        url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://music.163.com",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120, sock_read=60),
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.read()
        except Exception as error:  # noqa: BLE001 - network boundary
            logger.warning(f"[ai_cover] 网易云下载失败: {error}")
            return None
        # 过滤错误页/试听片段：非音频头或过小
        is_audio = data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
        if not is_audio or len(data) < 300_000:
            return None
        safe = _safe_filename_part(song_name, f"song{song_id}")
        path = self.download_dir / f"{safe}_{song_id}.mp3"
        try:
            path.write_bytes(data)
        except OSError as error:
            logger.warning(f"[ai_cover] 保存下载文件失败: {error}")
            return None
        return str(path), path.name

    # ─── QQ 音乐备源 ─────────────────────────────────────
    #
    # 网易云对缺版权的歌（抖音热歌、周杰伦等）连音频文件都没有，酷狗/酷我的免费
    # 接口已死。qqmusic-api（含签名算法）可用：搜索全通，付费歌需要登录凭据，
    # 普通账号能下"可完整试听"的歌，绿针权限更大。凭据从插件配置 qqmusic_cookie 读。

    def _qq_credential(self):
        """从配置的 QQ 音乐 cookie 构造凭据；未配置返回 None（匿名）。"""
        raw = str(self.config.get("qqmusic_cookie", "") or "").strip()
        if not raw:
            return None

        def grab(key: str) -> str:
            m = re.search(rf"{key}=([^;\s]+)", raw)
            return m.group(1) if m else ""

        try:
            from qqmusic_api.models.request import Credential
        except ImportError as error:
            logger.warning(f"[ai_cover] qqmusic_api 未安装，QQ 音乐凭据不可用: {error}")
            return None
        musickey = grab("qm_keyst") or grab("musickey")
        if not musickey:
            logger.warning("[ai_cover] qqmusic_cookie 里没有 qm_keyst/musickey，QQ 音乐按匿名访问")
            return None
        uin = grab("uin") or grab("wxuin") or grab("musicid")
        try:
            return Credential(
                musicid=int(uin) if uin.isdigit() else 0,
                musickey=musickey,
                openid=grab("openid"),
            )
        except (TypeError, ValueError) as error:
            logger.warning(f"[ai_cover] QQ 音乐凭据解析失败: {error}")
            return None

    async def _qq_search(self, keyword: str) -> list[dict]:
        """QQ 音乐歌曲搜索，返回与网易云同形状的候选（{id,name,artists}），失败返回 None。"""
        try:
            from qqmusic_api import Client
        except ImportError as error:
            logger.warning(f"[ai_cover] qqmusic_api 未安装，跳过 QQ 音乐源: {error}")
            return None
        try:
            async with Client(credential=self._qq_credential()) as client:
                page = await client.search.search_by_type(keyword, num=5, highlight=False)
                return [
                    {
                        "id": it.mid,
                        "name": it.name,
                        "artists": [{"name": s.name} for s in (it.singer or [])],
                    }
                    for it in (page.song or [])
                ][:5]
        except Exception as error:  # noqa: BLE001 - network boundary
            logger.warning(f"[ai_cover] QQ 音乐搜索失败: {error}")
            return None

    async def _qq_download(self, song_mid, song_name: str) -> tuple[str, str] | None:
        """通过 QQ 音乐获取播放链接并下载，返回 (本地路径, 文件名)；无权限返回 None。"""
        try:
            from qqmusic_api import Client
            from qqmusic_api.modules.song import SongFileType, SongFileInfo
        except ImportError as error:
            logger.warning(f"[ai_cover] qqmusic_api 未安装，跳过 QQ 音乐源: {error}")
            return None
        try:
            async with Client(credential=self._qq_credential()) as client:
                infos = [SongFileInfo(mid=song_mid, file_type=SongFileType.MP3_128)]
                resp = await client.song.get_song_urls(infos, SongFileType.MP3_128)
                item = resp.data[0]
                if not item.purl or item.result != 0:
                    logger.info(
                        f"[ai_cover] QQ 音乐无下载权限 (result={item.result})，需登录凭据或该歌为 VIP"
                    )
                    return None
                url = f"https://isure.stream.qqmusic.qq.com/{item.purl}"
        except Exception as error:  # noqa: BLE001 - network boundary
            logger.warning(f"[ai_cover] QQ 音乐获取链接失败: {error}")
            return None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://y.qq.com/",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120, sock_read=60),
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.read()
        except Exception as error:  # noqa: BLE001 - network boundary
            logger.warning(f"[ai_cover] QQ 音乐下载失败: {error}")
            return None
        is_audio = data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
        if not is_audio or len(data) < 300_000:
            return None
        safe = _safe_filename_part(song_name, f"qq{song_mid}")
        path = self.download_dir / f"qq_{safe}_{_safe_filename_part(str(song_mid), 'x')}.mp3"
        try:
            path.write_bytes(data)
        except OSError as error:
            logger.warning(f"[ai_cover] 保存下载文件失败: {error}")
            return None
        return str(path), path.name

    @staticmethod
    def _match_candidate(songs: list[dict], song_name: str) -> tuple[dict | None, str]:
        """从候选里选歌：优先"搜索词里的歌手名"命中的候选。

        返回 (候选, 第一个像歌手名却没匹配上的词)。匹配不上时候选为 None，
        由调用方决定回退到第一结果还是报无版权。
        """
        def _artists_of(s: dict) -> str:
            return "/".join(a.get("name", "") for a in (s.get("artists") or []))

        words = [w for w in song_name.split() if len(w) >= 2]
        missing_artist = ""
        for word in words:
            matched = [s for s in songs if word.lower() in _artists_of(s).lower()]
            if matched:
                return matched[0], ""
            if any(word.lower() in (s.get("name") or "").lower() for s in songs):
                # 这个词像歌名的一部分（候选歌名里出现了），不算歌手名
                continue
            missing_artist = missing_artist or word
        return None, missing_artist

    async def _make_and_send_cover(
        self,
        event: AstrMessageEvent,
        audio_path: str,
        original_name: str,
        model_name: str,
        vocal_transpose: int,
        instrumental_transpose: int,
        speed: float,
        source_key: str | None,
        source_label: str = "",
    ) -> tuple[bool, str]:
        """公共翻唱流程：防重复 → 制作 → 语音发送（失败回退 MP3 文件）。

        不发任何过程文字，成功时只发一条语音/文件。返回 (是否成功, 给调用方的说明)。
        source_label：伴奏来源的"歌名 - 歌手"标注，让用户知道用的是谁的版本。
        """
        if (
            source_key
            and source_key == self._last_cover_key
            and time.time() - self._last_cover_at < 900
        ):
            return True, "刚刚已经发过了，别再让我唱啦。"
        try:
            await asyncio.to_thread(self._cleanup_outputs_sync)
            output, actual_model, _index, _cache_hit = await self._request_cover(
                audio_path,
                original_name,
                model_name,
                vocal_transpose,
                instrumental_transpose,
                self._configured_gain("vocal_gain", DEFAULT_VOCAL_GAIN),
                self._configured_gain("instrumental_gain", DEFAULT_INSTRUMENTAL_GAIN),
                speed,
            )
        except asyncio.TimeoutError:
            return False, f"AI 翻唱超时（{self.timeout_seconds} 秒），请检查 RVC 服务。"
        except Exception as error:  # noqa: BLE001 - LLM tool boundary
            return False, f"AI 翻唱失败：{error}"

        sent = False
        if bool(self.config.get("send_as_record", False)):
            record_chain = [OriginalFormatRecord.fromFileSystem(output)]
            for attempt in range(2):
                try:
                    await event.send(event.chain_result(record_chain))
                    sent = True
                    break
                except Exception as send_error:  # noqa: BLE001
                    logger.warning(
                        f"[ai_cover] 语音发送失败(第{attempt + 1}次): "
                        f"{type(send_error).__name__} {send_error}"
                    )
                    await asyncio.sleep(2)
        if not sent:
            file_chain = [
                File(
                    name=_cover_filename(actual_model, original_name),
                    file=str(output),
                )
            ]
            for attempt in range(2):
                try:
                    await event.send(event.chain_result(file_chain))
                    sent = True
                    break
                except Exception as send_error:  # noqa: BLE001
                    logger.warning(
                        f"[ai_cover] 文件发送失败(第{attempt + 1}次): "
                        f"{type(send_error).__name__} {send_error}"
                    )
                    await asyncio.sleep(2)
        if not sent:
            return False, "翻唱做好了，但发送失败了，请稍后再试一次。"
        if source_key:
            self._last_cover_key = source_key
            self._last_cover_at = time.time()
        version_note = f"，伴奏版本：{source_label}" if source_label else ""
        mood_note = self._mood_remark()
        return True, (f"翻唱MP3已发出（{actual_model}音色{version_note}）。{mood_note}"
                      f"已发送过一次，不要重复调用本工具。")

    def _mood_remark(self) -> str:
        """识海心境耦合（论文§13.2：RVC 翻唱受 love/happy 驱动）。

        读识海心智核的状态文件，翻唱成功后给 LLM 一句心境提示，
        让"这首歌此刻对我意味着什么"由动力学状态决定。
        """
        if not bool(self.config.get("mood_remark_enabled", True)):
            return ""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            path = os.path.join(str(get_astrbot_plugin_data_path()),
                                "shihai", "mind_state.json")
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            e = data.get("e", {})
            love = float(e.get("love", 0.0))
            happy = float(e.get("happy", 0.0))
            sad = float(e.get("sad", 0.0))
            if love >= 0.5:
                return "心境提示：此刻对主人依恋很浓，这首唱得格外走心，回复里可以自然带一句此刻的感受。"
            if happy >= 0.5:
                return "心境提示：此刻很开心，回复可以雀跃一点。"
            if sad >= 0.5:
                return "心境提示：此刻有点低落，这首歌像在替自己说话，回复可以安静一点。"
        except Exception:  # noqa: BLE001 - 心境只是锦上添花，读不到就跳过
            pass
        return ""

    @filter.llm_tool("cover_tune")
    async def cover_tune(self, event: AstrMessageEvent, key: str, value: str):
        """调整AI翻唱插件的参数（调参接口，改完立即生效并保存到配置）。可调参数：default_model(默认翻唱音色模型名)、index_rate(RVC特征索引占比,0~1)、vocal_gain(人声音量倍率,0~2)、instrumental_gain(伴奏音量倍率,0~2)、send_as_record(true=发语音/false=发MP3文件)、output_retention_hours(结果保留小时数,1~168)、mood_remark_enabled(识海心境提示开关true/false)。主人说"唱歌声音大一点""换个默认音色""下次发文件别发语音"这类要求时用。

        Args:
            key (str): 参数名
            value (str): 目标值（数字或 true/false）
        """
        key = str(key or "").strip()
        ranges = {"index_rate": (0.0, 1.0), "vocal_gain": (0.0, 2.0),
                  "instrumental_gain": (0.0, 2.0)}
        floats = set(ranges) | set()
        if key == "default_model":
            val = str(value or "").strip()
            if not val:
                return "模型名不能为空。"
        elif key in floats:
            try:
                lo, hi = ranges[key]
                val = max(lo, min(hi, float(str(value).strip())))
            except (TypeError, ValueError):
                return f"参数值 {value!r} 不是数字。"
        elif key == "output_retention_hours":
            try:
                val = max(1, min(168, int(float(str(value).strip()))))
            except (TypeError, ValueError):
                return f"参数值 {value!r} 不是数字。"
        elif key in ("send_as_record", "mood_remark_enabled"):
            val = str(value).strip().lower() in ("1", "true", "yes", "on")
        else:
            return (f"没有叫 {key} 的参数。可调：default_model、index_rate、"
                    "vocal_gain、instrumental_gain、send_as_record、"
                    "output_retention_hours、mood_remark_enabled。")
        old = self.config.get(key)
        self.config[key] = val
        try:
            self.config.save_config()
        except Exception:  # noqa: BLE001
            pass
        return f"已把 {key} 从 {old} 调成 {val}，立即生效。"

    @filter.llm_tool()
    async def cover_song_by_name(
        self,
        event: AstrMessageEvent,
        song_name: str,
        model: str = "",
        vocal_transpose: str = "0",
        instrumental_transpose: str = "0",
        speed: str = "1",
    ):
        """按歌名搜索歌曲、自动下载并制作成 AI 翻唱语音（完整歌曲、带原曲伴奏）发给用户。当用户想听一首完整的、有伴奏的歌时调用——包括"翻唱一首XX"、"用你的声音唱XX"、"来一首XX"、"唱首XX"这类想听整首歌的请求（用户没说"清唱/哼两句"就优先用本工具）。
        随口清唱几句请用 dafeiyu_sing；用户已经发来了音频文件时不要用本工具，改用 cover_audio。
        重要：song_name 必须带上你知道的原唱歌手名；如果第一首因版权下载失败，换其他歌手的版本重试后，回复里必须明确告诉用户"原唱版曲库没版权，这次用的是XX版伴奏"。
        Args:
            song_name(string): 歌曲名，尽量写成"歌名 原唱歌手"，如"晴天 周杰伦"。带原唱歌手名能避免搜到翻唱/山寨版；不带时可能搜到别人的版本。
            model(string): 音色模型名，如 大肥鱼（你自己的声音）、芙宁娜；留空使用默认音色（大肥鱼）。
            vocal_transpose(string): 人声变调半音数（-24~24 的整数），不写为 0。
            instrumental_transpose(string): 背景音乐变调半音数（-24~24 的整数），一般与人声相同。
            speed(string): 变速倍率 0.5~2.0 的数字，1 为原速。
        """
        try:
            vt = int(str(vocal_transpose).strip() or 0)
            it = int(str(instrumental_transpose).strip() or 0)
            sp = float(str(speed).strip() or 1)
        except (TypeError, ValueError):
            vt, it, sp = 0, 0, 1.0
        song_name = (song_name or "").strip()
        if not song_name:
            return "没说歌名呀，告诉我唱哪首。"
        model_name = model.strip() or self._configured_model()

        # 从搜索词里识别"歌手名"：只有它出现在某些候选的歌手字段里才算数。
        # 目的：曲库里有大量山寨/翻唱号（如冒名 "Jay"），优先选歌手名命中的正版候选。
        def _artists_of(s: dict) -> str:
            return "/".join(a.get("name", "") for a in (s.get("artists") or []))

        # 主源：网易云。搜不到或下载失败（缺版权，如抖音热歌/周杰伦）时换 QQ 音乐备源。
        missing_artist = ""
        song = None
        artists = ""
        title = song_name
        searched_any = False
        downloaded = None
        songs = await self._netease_search(song_name)
        if songs:
            searched_any = True
            song, missing_artist = self._match_candidate(songs, song_name)
            if song is None:
                song = songs[0]
            artists = _artists_of(song)
            title = f"{song.get('name', '未知')} - {artists}" if artists else song.get("name", "未知")
            downloaded = await self._netease_download(song.get("id"), song.get("name") or "song")
        if not downloaded:
            songs = await self._qq_search(song_name)
            if songs:
                searched_any = True
                song, _ = self._match_candidate(songs, song_name)
                if song is None:
                    song = songs[0]
                artists = _artists_of(song)
                title = (
                    f"{song.get('name', '未知')} - {artists}（QQ音乐源）"
                    if artists
                    else f"{song.get('name', '未知')}（QQ音乐源）"
                )
                downloaded = await self._qq_download(song.get("id"), song.get("name") or "song")
        if not searched_any:
            return f"没搜到《{song_name}》，换个歌名或者加上歌手名试试。"
        if not downloaded:
            words = [w for w in song_name.split() if len(w) >= 2]
            if missing_artist and len(words) > 1:
                return (
                    f"没找到 {missing_artist} 演唱的正版《{song_name.split()[0]}》——"
                    "网易云和QQ音乐都没有可用音源，这个曲源很可能全是VIP锁或翻唱版。"
                    "换一首有版权的歌，或者你直接把音频文件发给我，我照样能用你的音色翻唱。"
                )
            return (
                f"《{title}》暂时没有可用的音频源（版权限制），换一首试试吧。"
                "你也可以换这首歌的其他歌手版本重试，但事后必须告诉用户用的是谁的版本。"
            )
        audio_path, original_name = downloaded
        try:
            st = os.stat(audio_path)
            source_key = f"{model_name}|{Path(original_name).name}|{st.st_size}"
        except OSError:
            source_key = None
        _ok, message = await self._make_and_send_cover(
            event, audio_path, original_name, model_name, vt, it, sp, source_key,
            source_label=f"《{song.get('name', '未知')}》-{artists or '未知歌手'}",
        )
        if _ok:
            # §7.5 bot 侧时序锚：翻唱成功写入行动日志（core 消费成记忆）
            self._log_cover_action(f"我给主人唱了一首《{song.get('name', '未知')}》")
        return message

    def _log_cover_action(self, text: str) -> None:
        """把翻唱动作追加给识海核心（core 每 tick 消费成图谱 bot 节点）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
            p = os.path.join(str(get_astrbot_plugin_data_path()),
                             "shihai", "action_log.jsonl")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as fp:
                fp.write(json.dumps({"ts": time.time(), "text": text},
                                    ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ai_cover] 行动日志写入失败: {type(e).__name__} {e}")

    @filter.llm_tool()
    async def tune_cover_params(
        self,
        event: AstrMessageEvent,
        vocal_gain: str = "",
        instrumental_gain: str = "",
        index_rate: str = "",
        rms_mix_rate: str = "",
        protect: str = "",
    ):
        """查看或调整自己的翻唱音质参数。主人反馈"声音沙哑/有毛刺/人声太小/伴奏太响/不像你的声音"时调用。
        不带任何参数 = 只查看当前参数值；带参数 = 修改对应项（只改传入的项，其他项不动）。
        修改成功后，主动用 cover_song_by_name 重翻同一首歌发给主人对比效果。

        Args:
            vocal_gain(string): 人声音量 0~2（当前1.2），嫌人声小调大、嫌刺耳调小。不调整就留空。
            instrumental_gain(string): 伴奏音量 0~2（当前1.0），嫌伴奏吵就调小。不调整就留空。
            index_rate(string): 音色检索强度 0~1（当前0.85），毛刺/电流感多就调低（如0.6），不够像自己就调高。不调整就留空。
            rms_mix_rate(string): 响度融合 0~1（当前0.25），值越大气口越平稳但声音越平。不调整就留空。
            protect(string): 清辅音保护 0~0.5（当前0.35），齿音嘶哑就调大（如0.45），咬字含糊就调小。不调整就留空。
        """
        ranges = {
            "vocal_gain": (0.0, 2.0),
            "instrumental_gain": (0.0, 2.0),
            "index_rate": (0.0, 1.0),
            "rms_mix_rate": (0.0, 1.0),
            "protect": (0.0, 0.5),
        }
        defaults = {
            "vocal_gain": 1.2,
            "instrumental_gain": 1.0,
            "index_rate": 0.85,
            "rms_mix_rate": 0.25,
            "protect": 0.35,
        }
        raw_inputs = {
            "vocal_gain": vocal_gain,
            "instrumental_gain": instrumental_gain,
            "index_rate": index_rate,
            "rms_mix_rate": rms_mix_rate,
            "protect": protect,
        }
        changed: dict[str, float] = {}
        errors: list[str] = []
        for key, raw in raw_inputs.items():
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                value = float(text)
            except (TypeError, ValueError):
                errors.append(f"{key} 不是数字（{text}）")
                continue
            if not math.isfinite(value):
                errors.append(f"{key} 数值无效（{text}）")
                continue
            lo, hi = ranges[key]
            clamped = max(lo, min(hi, value))
            if clamped != value:
                errors.append(f"{key}={value:g} 超出范围 [{lo:g}, {hi:g}]，已按 {clamped:g} 处理")
            changed[key] = round(clamped, 2)
        snapshot = {
            key: round(float(self.config.get(key, defaults[key])), 2)
            for key in defaults
        }
        if not changed and not errors:
            lines = [f"{key} = {snapshot[key]}" for key in snapshot]
            return "当前翻唱参数：\n" + "\n".join(lines)
        if changed:
            self.config.update(changed)
            try:
                self.config.save_config()
            except Exception as save_error:  # noqa: BLE001 - tool boundary
                logger.warning(f"[ai_cover] 保存翻唱参数失败: {save_error}")
            snapshot.update(changed)
        if changed:
            reply = "已调整翻唱参数：" + "，".join(f"{k} → {changed[k]}" for k in changed)
        else:
            reply = "翻唱参数没有改动"
        reply += "\n当前参数：\n" + "\n".join(f"{k} = {snapshot[k]}" for k in snapshot)
        if errors:
            reply += "\n注意：" + "；".join(errors)
        reply += "\n接下来用 cover_song_by_name 重翻同一首歌，让主人对比效果。"
        return reply

    @filter.llm_tool()
    async def cover_audio(
        self,
        event: AstrMessageEvent,
        model: str = "",
        vocal_transpose: str = "0",
        instrumental_transpose: str = "0",
        speed: str = "1.0",
    ):
        """把主人发来的语音/歌曲文件制作成 AI 翻唱并发送。主人发完音频后说"翻唱"、"帮我翻唱这首歌"时调用。
        主人还没发音频时不要调用本工具，先提醒主人把歌曲（MP3/语音）发过来。

        Args:
            model(string): 翻唱音色模型名，如 大肥鱼（你自己的声音）、芙宁娜；留空使用默认音色（大肥鱼）。
            vocal_transpose(string): 人声变调半音数（-24~24 的整数），女转男建议 -12，男转女建议 +12，不写为 0。
            instrumental_transpose(string): 背景音乐变调半音数（-24~24 的整数），一般与人声相同。
            speed(string): 变速倍率 0.5~2.0 的数字，1 为原速。
        """
        try:
            vt = int(str(vocal_transpose).strip() or 0)
            it = int(str(instrumental_transpose).strip() or 0)
            sp = float(str(speed).strip() or 1)
        except (TypeError, ValueError):
            vt, it, sp = 0, 0, 1.0
        try:
            audio_path, original_name = await self._find_audio(event)
        except Exception as error:  # noqa: BLE001 - LLM tool boundary
            return f"没有找到要翻唱的音频：{error}"
        model_name = model.strip() or self._configured_model()
        try:
            st = os.stat(audio_path)
            source_key = f"{model_name}|{Path(original_name).name}|{st.st_size}"
        except OSError:
            source_key = None
        _ok, message = await self._make_and_send_cover(
            event, audio_path, original_name, model_name, vt, it, sp, source_key
        )
        return message

    @filter.command("翻唱")
    async def cover(
        self,
        event: AstrMessageEvent,
        model: str = "",
        vocal_transpose: int = 0,
        instrumental_transpose: int = 0,
        vocal_gain: float = -1.0,
        instrumental_gain: float = -1.0,
        speed: float = 1.0,
    ):
        """制作 AI 翻唱，省略模型时使用插件配置的默认模型。"""
        async for result in self._run_cover(
            event,
            model,
            vocal_transpose,
            instrumental_transpose,
            vocal_gain,
            instrumental_gain,
            speed,
            force_file=False,
        ):
            yield result

    @filter.command("翻唱下载")
    async def cover_download(
        self,
        event: AstrMessageEvent,
        model: str = "",
        vocal_transpose: int = 0,
        instrumental_transpose: int = 0,
        vocal_gain: float = -1.0,
        instrumental_gain: float = -1.0,
        speed: float = 1.0,
    ):
        """制作 AI 翻唱并始终发送 MP3 文件。"""
        async for result in self._run_cover(
            event,
            model,
            vocal_transpose,
            instrumental_transpose,
            vocal_gain,
            instrumental_gain,
            speed,
            force_file=True,
        ):
            yield result

    @filter.command("翻唱状态")
    async def cover_status(self, event: AstrMessageEvent):
        """检查 RVC 服务状态。"""
        try:
            payload = await self._get_json("/health")
            state = "正在处理任务" if payload.get("busy") else "空闲"
            yield event.plain_result(
                f"RVC 服务正常：{state}，可用模型 {payload.get('models', 0)} 个。"
            )
        except Exception as error:  # noqa: BLE001 - status command reports connectivity errors
            yield event.plain_result(f"RVC 服务不可用：{error}")
