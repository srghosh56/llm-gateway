"""OpenAI-compatible endpoints: ``GET /v1/models`` and ``POST /v1/chat/completions``."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway import errors
from gateway.auth import TenantContext, authenticate_tenant
from gateway.backends import Backend, UpstreamError
from gateway.db import Database
from gateway.metrics import (
    TENANT_REQUESTS,
    TENANT_TOKENS,
    TIME_TO_FIRST_TOKEN,
    TOKENS_PER_SECOND,
    UPSTREAM_ERRORS,
    TenantLabeler,
)
from gateway.quota import record_usage
from gateway.registry import get_deployed, list_deployed
from gateway.schemas import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ModelCard,
    ModelList,
    Usage,
)

router = APIRouter(prefix="/v1", tags=["openai"])
log = logging.getLogger("gateway.chat")


async def _resolve(request: Request, model: str) -> tuple[Backend, str]:
    """Public model name -> (backend, upstream model id), via the registry.

    Only "deployed" models resolve; anything else is reported as not found, so clients
    cannot tell a retired model from one that never existed. One indexed query per request;
    a short TTL cache would be the next step under heavy load.
    """
    db: Database = request.app.state.db
    async with db.session() as session:
        entry = await get_deployed(session, model)
    backends: dict[str, Backend] = request.app.state.backends
    if entry is None or entry.backend not in backends:
        raise errors.model_not_found(model)
    return backends[entry.backend], entry.upstream_model


@dataclass
class Meter:
    """Everything that must happen once a completion ends: charge the quota, update metrics,
    write one summary log line. Shared by the streaming and non-streaming paths."""

    db: Database
    tenant: TenantContext
    tenant_label: str
    model: str
    backend: str
    started: float = field(default_factory=time.perf_counter)

    def upstream_failed(self, exc: UpstreamError) -> None:
        UPSTREAM_ERRORS.labels(self.backend, exc.reason).inc()
        log.warning("upstream error", extra={"backend": self.backend, "reason": exc.reason})

    async def finish(self, usage: Usage | None, outcome: str = "success") -> None:
        elapsed = time.perf_counter() - self.started
        TENANT_REQUESTS.labels(self.tenant_label, self.model, outcome).inc()
        if usage is not None:
            TENANT_TOKENS.labels(self.tenant_label, self.model, "prompt").inc(usage.prompt_tokens)
            TENANT_TOKENS.labels(self.tenant_label, self.model, "completion").inc(
                usage.completion_tokens
            )
            if usage.completion_tokens and elapsed > 0:
                TOKENS_PER_SECOND.labels(self.model).observe(usage.completion_tokens / elapsed)
            async with self.db.session() as session:
                await record_usage(session, self.tenant.id, usage.total_tokens)
        log.info(
            "completion",
            extra={
                "model": self.model,
                "outcome": outcome,
                "prompt_tokens": usage.prompt_tokens if usage else 0,
                "completion_tokens": usage.completion_tokens if usage else 0,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )


@router.get("/models")
async def list_models(
    request: Request, _: TenantContext = Depends(authenticate_tenant)
) -> ModelList:
    db: Database = request.app.state.db
    async with db.session() as session:
        deployed = await list_deployed(session)
    return ModelList(
        data=[
            ModelCard(id=m.name, created=int(m.created_at.timestamp()), owned_by=m.owner)
            for m in deployed
        ]
    )


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    tenant: TenantContext = Depends(authenticate_tenant),
) -> ChatCompletion | StreamingResponse:
    backend, upstream_model = await _resolve(request, body.model)
    labeler: TenantLabeler = request.app.state.tenant_labeler
    meter = Meter(request.app.state.db, tenant, labeler(tenant.name), body.model, backend.name)

    if not body.stream:
        try:
            completion = await backend.complete(body, upstream_model)
        except UpstreamError as exc:
            meter.upstream_failed(exc)
            await meter.finish(None, "upstream_error")
            raise errors.upstream_error() from exc
        completion.model = body.model  # clients see the gateway's name, not the upstream id
        await meter.finish(completion.usage)
        return completion

    chunks = backend.stream(body, upstream_model)
    # Pull the first chunk *before* sending headers: if the upstream is down we can still
    # answer with a proper 502 JSON error instead of a 200 stream that dies immediately.
    try:
        first: ChatCompletionChunk | None = await anext(chunks)
    except StopAsyncIteration:
        first = None
    except UpstreamError as exc:
        meter.upstream_failed(exc)
        await meter.finish(None, "upstream_error")
        raise errors.upstream_error() from exc
    TIME_TO_FIRST_TOKEN.labels(body.model).observe(time.perf_counter() - meter.started)

    include_usage = bool(body.stream_options and body.stream_options.include_usage)
    return StreamingResponse(
        _sse(first, chunks, body.model, include_usage, meter),
        media_type="text/event-stream",
        # Disable proxy buffering so tokens reach the client as they are generated.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse(
    first: ChatCompletionChunk | None,
    rest: AsyncIterator[ChatCompletionChunk],
    public_model: str,
    include_usage: bool,
    meter: Meter,
) -> AsyncIterator[str]:
    """Re-emit upstream chunks as Server-Sent Events, ending with ``data: [DONE]``."""
    usage: Usage | None = None
    content_chunks = 0
    outcome = "success"

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
    except UpstreamError as exc:
        # Headers are already sent, so the status code can't change; emit an error event.
        meter.upstream_failed(exc)
        outcome = "upstream_error"
        err = errors.upstream_error("Upstream failed mid-stream.")
        yield f"data: {json.dumps(err.body())}\n\n"
    except BaseException:
        outcome = "client_disconnected"  # cancelled/closed because the client went away
        raise
    finally:
        # Runs on normal end, upstream failure *and* client disconnect. If the upstream never
        # reported usage (e.g. client hung up early), estimate ~1 token per content chunk so
        # partial streams are still charged.
        if usage is None and content_chunks:
            usage = Usage(completion_tokens=content_chunks, total_tokens=content_chunks)
        # shield(): on disconnect this generator is cancelled; the write must still finish.
        await asyncio.shield(meter.finish(usage, outcome))
    yield "data: [DONE]\n\n"
