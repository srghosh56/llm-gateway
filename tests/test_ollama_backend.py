"""OllamaBackend tests against a fake upstream (httpx.MockTransport): no Ollama needed."""

import json

import httpx
import pytest

from gateway.backends import OllamaBackend, UpstreamError
from gateway.schemas import ChatCompletionRequest

REQ = ChatCompletionRequest.model_validate(
    {"model": "public-name", "messages": [{"role": "user", "content": "hi"}]}
)


def backend_with(handler: httpx.MockTransport) -> OllamaBackend:
    b = OllamaBackend("http://ollama/v1")
    b._client = httpx.AsyncClient(transport=handler, base_url="http://ollama/v1")
    return b


async def test_complete_forwards_upstream_model_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen2.5:0.5b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hey"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    result = await backend_with(httpx.MockTransport(handler)).complete(REQ, "qwen2.5:0.5b")
    assert seen["model"] == "qwen2.5:0.5b"
    assert result.choices[0].message.content == "hey"


async def test_stream_parses_sse_and_requests_usage() -> None:
    seen: dict[str, object] = {}
    chunk = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": "hi"}}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        sse = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    out = [c async for c in backend_with(httpx.MockTransport(handler)).stream(REQ, "m")]
    assert [c.choices[0].delta.content for c in out] == ["hi"]
    assert seen["stream_options"] == {"include_usage": True}


async def test_http_500_maps_to_upstream_error() -> None:
    backend = backend_with(httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(UpstreamError) as exc_info:
        await backend.complete(REQ, "m")
    assert exc_info.value.reason == "http_5xx"


async def test_connect_error_maps_to_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamError) as exc_info:
        await backend_with(httpx.MockTransport(handler)).complete(REQ, "m")
    assert exc_info.value.reason == "connect"
