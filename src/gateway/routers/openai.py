"""OpenAI-compatible endpoints: ``GET /v1/models`` and ``POST /v1/chat/completions``."""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway import errors
from gateway.auth import TenantContext, authenticate_tenant
from gateway.backends import Backend, UpstreamError
from gateway.config import Settings
from gateway.db import Database
from gateway.quota import record_usage
from gateway.schemas import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ModelCard,
    ModelList,
    Usage,
)

router = APIRouter(prefix="/v1", tags=["openai"])


def _resolve(request: Request, model: str) -> tuple[Backend, str]:
    settings: Settings = request.app.state.settings
    if model not in settings.models:
        raise errors.model_not_found(model)
    backend: Backend = request.app.state.backends[settings.default_backend]
    return backend, model


async def _account(db: Database, tenant: TenantContext, usage: Usage | None) -> None:
    """Charge the tenant's monthly quota with the tokens the backend reported."""
    if usage is None:
        return
    async with db.session() as session:
        await record_usage(session, tenant.id, usage.total_tokens)


@router.get("/models")
async def list_models(
    request: Request, _: TenantContext = Depends(authenticate_tenant)
) -> ModelList:
    settings: Settings = request.app.state.settings
    return ModelList(data=[ModelCard(id=m) for m in settings.models])


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    tenant: TenantContext = Depends(authenticate_tenant),
) -> ChatCompletion | StreamingResponse:
    backend, upstream_model = _resolve(request, body.model)

    if not body.stream:
        try:
            completion = await backend.complete(body, upstream_model)
        except UpstreamError as exc:
            raise errors.upstream_error() from exc
        completion.model = body.model  # clients see the gateway's name, not the upstream id
        await _account(request.app.state.db, tenant, completion.usage)
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
        _sse(first, chunks, body.model, include_usage, request.app.state.db, tenant),
        media_type="text/event-stream",
        # Disable proxy buffering so tokens reach the client as they are generated.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(
    first: ChatCompletionChunk | None,
    rest: AsyncIterator[ChatCompletionChunk],
    public_model: str,
    include_usage: bool,
    db: Database,
    tenant: TenantContext,
) -> AsyncIterator[str]:
    """Re-emit upstream chunks as Server-Sent Events, ending with ``data: [DONE]``."""
    usage: Usage | None = None
    content_chunks = 0

    async def all_chunks() -> AsyncIterator[ChatCompletionChunk]:
        if first is not None:
            yield first
        async for c in rest:
            yield c

    try:
        async for chunk in all_chunks():
            chunk.model = public_model
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.choices and chunk.choices[0].delta.content:
                content_chunks += 1
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
    finally:
        # Runs on normal end, upstream failure *and* client disconnect. If the upstream never
        # reported usage (e.g. client hung up early), estimate ~1 token per content chunk so
        # partial streams are still charged.
        if usage is None and content_chunks:
            usage = Usage(completion_tokens=content_chunks, total_tokens=content_chunks)
        # shield(): on disconnect this generator is cancelled; the write must still finish.
        await asyncio.shield(_account(db, tenant, usage))
    yield "data: [DONE]\n\n"
