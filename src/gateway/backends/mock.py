"""Deterministic backend for tests, CI and demos without a GPU.

It echoes the last user message. "Tokens" are whitespace-separated words, which keeps
quota and metrics tests exact and easy to reason about.
"""

import time
import uuid
from collections.abc import AsyncIterator

from gateway.backends.base import Backend, UpstreamError
from gateway.schemas import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatMessage,
    Choice,
    ChunkChoice,
    Delta,
    Usage,
)

# Upstream model id that always fails, so tests can exercise the upstream-error path.
FAILING_MODEL = "mock-error"


def count_tokens(text: str | None) -> int:
    return len(text.split()) if text else 0


def _reply_and_usage(request: ChatCompletionRequest) -> tuple[str, Usage]:
    last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
    reply = f"Echo: {last_user}"
    if request.max_tokens is not None:
        reply = " ".join(reply.split()[: request.max_tokens])
    prompt = sum(count_tokens(m.content) for m in request.messages)
    completion = count_tokens(reply)
    return reply, Usage(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


class MockBackend(Backend):
    name = "mock"

    async def complete(self, request: ChatCompletionRequest, upstream_model: str) -> ChatCompletion:
        if upstream_model == FAILING_MODEL:
            raise UpstreamError("mock_failure", "mock backend configured to fail")
        reply, usage = _reply_and_usage(request)
        return ChatCompletion(
            model=upstream_model,
            choices=[Choice(message=ChatMessage(role="assistant", content=reply))],
            usage=usage,
        )

    async def stream(
        self, request: ChatCompletionRequest, upstream_model: str
    ) -> AsyncIterator[ChatCompletionChunk]:
        if upstream_model == FAILING_MODEL:
            raise UpstreamError("mock_failure", "mock backend configured to fail")
        reply, usage = _reply_and_usage(request)
        chunk_id, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())

        def chunk(delta: Delta, finish: str | None = None) -> ChatCompletionChunk:
            return ChatCompletionChunk(
                id=chunk_id,
                created=created,
                model=upstream_model,
                choices=[ChunkChoice(delta=delta, finish_reason=finish)],
            )

        yield chunk(Delta(role="assistant", content=""))
        for i, word in enumerate(reply.split()):
            yield chunk(Delta(content=word if i == 0 else f" {word}"))
        # Final chunk: finish_reason + usage, mirroring OpenAI's include_usage behaviour.
        final = chunk(Delta(), finish="stop")
        final.usage = usage
        yield final
