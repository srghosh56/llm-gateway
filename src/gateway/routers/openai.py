"""OpenAI-compatible endpoints: ``GET /v1/models`` and ``POST /v1/chat/completions``."""

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from gateway import errors
from gateway.backends import Backend, UpstreamError
from gateway.config import Settings
from gateway.schemas import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ModelCard,
    ModelList,
)

router = APIRouter(prefix="/v1", tags=["openai"])


def _resolve(request: Request, model: str) -> tuple[Backend, str]:
    settings: Settings = request.app.state.settings
    if model not in settings.models:
        raise errors.model_not_found(model)
    backend: Backend = request.app.state.backends[settings.default_backend]
    return backend, model


@router.get("/models")
async def list_models(request: Request) -> ModelList:
    settings: Settings = request.app.state.settings
    return ModelList(data=[ModelCard(id=m) for m in settings.models])


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest, request: Request
) -> ChatCompletion | StreamingResponse:
    backend, upstream_model = _resolve(request, body.model)

    if not body.stream:
        try:
            completion = await backend.complete(body, upstream_model)
        except UpstreamError as exc:
            raise errors.upstream_error() from exc
        completion.model = body.model  # clients see the gateway's name, not the upstream id
        return completion

    chunks = backend.stream(body, upstream_model)
    # Pull the first chunk *before* sending headers: if the upstream is down we can still
    # answer with a proper 502 JSON error instead of a 200 stream that dies immediately.
    try:
        first: ChatCompletionChunk | None = await anext(chunks)
    except StopAsyncIteration:
        first = None
    except UpstreamError as exc:
        raise errors.upstream_error() from exc

    include_usage = bool(body.stream_options and body.stream_options.include_usage)
    return StreamingResponse(
        _sse(first, chunks, body.model, include_usage),
        media_type="text/event-stream",
        # Disable proxy buffering so tokens reach the client as they are generated.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(
    first: ChatCompletionChunk | None,
    rest: AsyncIterator[ChatCompletionChunk],
    public_model: str,
    include_usage: bool,
) -> AsyncIterator[str]:
    """Re-emit upstream chunks as Server-Sent Events, ending with ``data: [DONE]``."""

    async def all_chunks() -> AsyncIterator[ChatCompletionChunk]:
        if first is not None:
            yield first
        async for c in rest:
            yield c

    try:
        async for chunk in all_chunks():
            chunk.model = public_model
            if not include_usage:
                # We always request usage upstream (for metering) but only forward it if
                # the client asked, matching OpenAI semantics.
                if not chunk.choices:
                    continue
                chunk.usage = None
            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
    except UpstreamError:
        # Headers are already sent, so the status code can't change; emit an error event.
        err = errors.upstream_error("Upstream failed mid-stream.")
        yield f"data: {json.dumps(err.body())}\n\n"
    yield "data: [DONE]\n\n"
