"""LLM access for the cross-evaluation. The ONLY module that imports the Anthropic SDK.

``eval.py`` depends on the ``LLMClient`` protocol (text in, text out), so tests substitute a
fake with zero infra. The model string comes from config — never hardcoded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    async def complete(self, *, system: str, messages: Sequence[dict]) -> str: ...


class AnthropicLLM:
    def __init__(self, *, api_key: str, model: str, max_tokens: int) -> None:
        from anthropic import AsyncAnthropic  # lazy: tests use a fake, never import the SDK

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=list(messages),
        )
        return "".join(block.text for block in message.content if block.type == "text")
