"""LLM access. The ONLY module that imports the Anthropic SDK.

The companion brain depends on the ``LLMClient`` protocol (text in, text out), so
tests substitute a fake and later phases can swap the I/O without touching logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from anthropic import AsyncAnthropic


@runtime_checkable
class LLMClient(Protocol):
    def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]: ...

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str: ...


class AnthropicLLM:
    """Concrete client. Model string comes from config — never hardcoded."""

    def __init__(self, *, api_key: str, model: str, max_tokens: int) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=list(messages),
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=list(messages),
        )
        return "".join(block.text for block in message.content if block.type == "text")
