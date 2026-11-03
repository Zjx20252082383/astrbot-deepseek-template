"""大肥鱼画图插件：通义万相文生图（阿里云百炼，代金券抵扣）"""

import asyncio
import json
import time
import uuid
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

CREATE_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
DEFAULT_MODEL = "wanx2.1-t2i-turbo"
MAX_PROMPT_LEN = 400
PLUGIN_DIR = "astrbot_plugin_dafeiyu_draw"


@register("dafeiyu_draw", "zjx", "大肥鱼画图（通义万相）", "v1.0.0")
class DafeiyuDrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_DIR / "outputs"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _model(self) -> str:
        try:
            return str(self.config.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        except Exception:
            return DEFAULT_MODEL

    def _read_key(self) -> str:
        # 优先用插件配置里显式写的 key，留空则回退主配置的 aliyun_bailian_source
        try:
            cfg_key = str(self.config.get("api_key") or "").strip()
            if cfg_key:
                return cfg_key
        except Exception:
            pass
        try:
            p = Path(get_astrbot_plugin_data_path()).parent / "config" / "cmd_config.json"
            cfg = json.loads(p.read_text(encoding="utf-8-sig"))
            for src in cfg.get("provider_sources", []):
                if src.get("id") == "aliyun_bailian_source":
                    keys = src.get("key") or []
                    if keys:
                        return keys[0]
        except Exception as e:  # noqa: BLE001 - config boundary
            logger.warning(f"[dafeiyu_draw] 读 key 失败: {e}")
        return ""

    async def _generate(self, prompt: str) -> Path:
        key = self._read_key()
        if not key:
            raise RuntimeError("没有可用的百炼 key")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        body = {
            "model": self._model(),
            "input": {"prompt": prompt[:MAX_PROMPT_LEN]},
            "parameters": {"size": "1024*1024", "n": 1},
        }

        def _blocking() -> Path:
            import requests  # noqa: PLC0415

            r = requests.post(CREATE_URL, headers=headers, json=body, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"创建任务失败: {r.text[:150]}")
            task_id = r.json()["output"]["task_id"]
            for _ in range(40):
                time.sleep(3)
                q = requests.get(
                    TASK_URL.format(task_id=task_id),
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=15,
                )
                data = q.json()
                status = data.get("output", {}).get("task_status")
                if status == "SUCCEEDED":
                    results = data["output"].get("results") or []
                    if not results or not results[0].get("url"):
                        raise RuntimeError("任务成功但没有图片结果")
                    url = results[0]["url"]
                    img = requests.get(url, timeout=60)
                    img.raise_for_status()
                    out = self.data_dir / f"draw_{uuid.uuid4().hex[:12]}.png"
                    out.write_bytes(img.content)
                    return out
                if status == "FAILED":
                    raise RuntimeError(f"生成失败: {str(data)[:150]}")
            raise RuntimeError("生成超时")

        return await asyncio.to_thread(_blocking)

    @filter.llm_tool()
    async def dafeiyu_draw(self, event: AstrMessageEvent, prompt: str):
        """画一张图发给主人。主人说"画一张XX"、"给我画XX"、想要配图/插画/头像时调用。
        prompt 写具体的画面描述（主体 + 场景 + 风格），越具体画得越好，不要写真实人名。

        Args:
            prompt(string): 画面描述，如"一只蓝色小锦鲤在月光下的海面游泳，卡通风格"。
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return "还没说要画什么呀。"
        try:
            out = await self._generate(prompt)
        except Exception as e:  # noqa: BLE001 - tool boundary
            logger.warning(f"[dafeiyu_draw] 生成失败: {e}")
            return f"画图失败了：{e}"
        try:
            await event.send(event.chain_result([Image.fromFileSystem(str(out))]))
            return "画好了，图片已经发出去。"
        except Exception as e:  # noqa: BLE001 - send boundary
            logger.warning(f"[dafeiyu_draw] 图片发送失败: {e}")
            return "画画好了，但发送失败了，请主人稍后再试。"
