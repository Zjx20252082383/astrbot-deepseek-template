"""主动消息生成与投递（论文 §13.1：Behavior API → Platform Adapter）。

输入 = 人设一句 + 意图包 I_t + 简短固定指令，别无其他；
输出 = 一句要发给主人的话，经 context.send_message 投递。
"""

from __future__ import annotations

from typing import Optional

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain


class ProactiveSender:
    def __init__(self, context, owner_qid: str, platform_id: str):
        self.context = context
        self.owner_qid = owner_qid
        self.platform_id = platform_id

    @property
    def target(self) -> str:
        return f"{self.platform_id}:FriendMessage:{self.owner_qid}"

    async def generate(self, provider, system: str) -> str:
        resp = await provider.text_chat(
            prompt="（把你此刻想对主人说的话说出来）",
            system_prompt=system,
        )
        text = ""
        if resp is not None:
            for attr in ("result_chain", "completion_text"):
                v = getattr(resp, attr, None)
                if v:
                    text = v.get_plain_text() if hasattr(v, "get_plain_text") else str(v)
                    if text.strip():
                        break
        return (text or "").strip().strip('"“”').strip()

    async def send(self, text: str) -> bool:
        try:
            chain = MessageChain(chain=[Plain(text=text)])
            ok = await self.context.send_message(self.target, chain)
            if ok is False:
                raise RuntimeError("send_message 返回 False")
            return True
        except Exception:
            try:
                ok = await self.context.send_message(self.target, text)
                return ok is not False
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识海行为] 发送失败: {type(e).__name__} {e}")
                return False
