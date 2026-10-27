"""Compatibility test: drive the gateway with the official OpenAI Python SDK."""

import httpx
import openai
import pytest
from fastapi import FastAPI

from tests.conftest import TenantHandle


def sdk(app: FastAPI, key: str) -> openai.AsyncOpenAI:
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return openai.AsyncOpenAI(
        api_key=key, base_url="http://test/v1", http_client=http, max_retries=0
    )


async def test_sdk_models_and_completion(app: FastAPI, tenant: TenantHandle) -> None:
    client = sdk(app, tenant.key)
    models = await client.models.list()
    assert {m.id for m in models.data} == {"mock-echo", "mock-error"}

    resp = await client.chat.completions.create(
        model="mock-echo", messages=[{"role": "user", "content": "hello sdk"}]
    )
    assert resp.choices[0].message.content == "Echo: hello sdk"
    assert resp.usage is not None and resp.usage.total_tokens == 5


async def test_sdk_streaming(app: FastAPI, tenant: TenantHandle) -> None:
    client = sdk(app, tenant.key)
    stream = await client.chat.completions.create(
        model="mock-echo",
        messages=[{"role": "user", "content": "stream me"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, usage = "", None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
        if chunk.usage:
            usage = chunk.usage
    assert text == "Echo: stream me"
    assert usage is not None and usage.completion_tokens == 3


async def test_sdk_raises_typed_errors(app: FastAPI, tenant: TenantHandle) -> None:
    with pytest.raises(openai.AuthenticationError):
        await sdk(app, "gw_wrong").models.list()
    with pytest.raises(openai.NotFoundError):
        await sdk(app, tenant.key).chat.completions.create(
            model="nope", messages=[{"role": "user", "content": "x"}]
        )
