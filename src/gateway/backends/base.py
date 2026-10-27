"""Backend interface.

The gateway talks to every model provider through this small interface. Requests and responses
use the OpenAI schema end to end, so a backend only has to translate if its upstream is not
OpenAI-compatible. Adding vLLM or a hosted API means one new subclass, nothing else changes.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from gateway.schemas import ChatCompletion, ChatCompletionChunk, ChatCompletionRequest


class UpstreamError(Exception):
    """Raised when the upstream fails. ``reason`` is a small fixed vocabulary used as a
    metric label (timeout, connect, http_5xx ...), so it must never contain free text."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Backend(ABC):
    name: str

    @abstractmethod
    async def complete(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> ChatCompletion: ...

    @abstractmethod
    def stream(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Yield chunks. The last chunk should carry ``usage`` so tokens can be metered."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook, default no-op
        """Release connections on shutdown."""
