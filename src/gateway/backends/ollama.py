"""Backend that forwards to Ollama's OpenAI-compatible endpoint (``/v1/chat/completions``).

Because Ollama already speaks the OpenAI protocol this is a thin proxy: swap the model name
for the upstream id, forward, and parse the response back into our schema (which validates
it, so a misbehaving upstream fails loudly here instead of inside a client).
"""

import json
from collections.abc import AsyncIterator

import httpx

from gateway.backends.base import Backend, UpstreamError
from gateway.schemas import ChatCompletion, ChatCompletionChunk, ChatCompletionRequest


def _classify(exc: httpx.HTTPError) -> UpstreamError:
    # Map transport errors to a fixed set of reasons (bounded metric label values).
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError("timeout", str(exc) or "upstream timeout")
    if isinstance(exc, httpx.ConnectError):
        return UpstreamError("connect", str(exc) or "cannot connect to upstream")
    return UpstreamError("transport", str(exc) or "upstream transport error")


class OllamaBackend(Backend):
    name = "ollama"

    def __init__(self, base_url: str, timeout_seconds: float = 120.0) -> None:
        # One pooled client for the app's lifetime: reuses TCP connections to Ollama.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
        )

    def _payload(self, request: ChatCompletionRequest, upstream_model: str) -> dict[str, object]:
        payload = request.model_dump(exclude_none=True)
        payload["model"] = upstream_model
        return payload

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            body = (await response.aread()).decode(errors="replace")[:500]
            reason = "http_5xx" if response.status_code >= 500 else "http_4xx"
            raise UpstreamError(reason, f"upstream returned {response.status_code}: {body}")

    async def complete(self, request: ChatCompletionRequest, upstream_model: str) -> ChatCompletion:
        payload = self._payload(request, upstream_model)
        payload["stream"] = False
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise _classify(exc) from exc
        await self._raise_for_status(response)
        return ChatCompletion.model_validate(response.json())

    async def stream(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        payload = self._payload(request, upstream_model)
        payload["stream"] = True
        # Always ask for usage so the gateway can meter streamed tokens.
        payload["stream_options"] = {"include_usage": True}
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                await self._raise_for_status(resp)
                # SSE: each event is a "data: <json>" line; "data: [DONE]" ends the stream.
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    yield ChatCompletionChunk.model_validate(json.loads(data))
        except httpx.HTTPError as exc:
            raise _classify(exc) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
