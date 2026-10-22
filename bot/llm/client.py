# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端(支持 Function Calling 工具调用)。

兼容各家端点(chat/completions + tools):
- 智谱 GLM-4-Flash(免费):   https://open.bigmodel.cn/api/paas/v4
- 腾讯混元:                 https://api.hunyuan.cloud.tencent.com/v1
- DeepSeek:                 https://api.deepseek.com/v1
"""

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from bot.config import LLMConfig

logger = logging.getLogger("llm")


class LLMClient:
    """基于 aiohttp 的 OpenAI 兼容客户端,携带节流与错误处理。"""

    def __init__(self, cfg: LLMConfig):
        self._cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def enabled(self) -> bool:
        """配置有效(启用 + 填了真实 key)才算可用。"""
        return (
            self._cfg.enable
            and bool(self._cfg.api_key)
            and not self._cfg.api_key.startswith("在这里")
        )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._cfg.timeout),
                headers={
                    "Authorization": f"Bearer {self._cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """调用 chat/completions,返回完整响应 JSON。

        :param tools: OpenAI function calling 工具 schema 列表(可选)
        """
        session = await self._ensure_session()
        url = f"{self._cfg.base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "max_tokens": max_tokens or self._cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM HTTP {resp.status}: {text[:300]}")
            return await resp.json()
