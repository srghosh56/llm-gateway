import json

import httpx

CHAT = "/v1/chat/completions"


def body(model: str = "mock-echo", stream: bool = False, **extra: object) -> dict[str, object]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello there gateway"}],
        "stream": stream,
        **extra,
    }


def parse_sse(text: str) -> list[str]:
    return [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]


async def test_list_models(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/models")
    assert r.status_code == 200
    assert r.json()["object"] == "list"
    assert {m["id"] for m in r.json()["data"]} == {"mock-echo", "mock-error"}


async def test_non_stream_completion(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body())
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "mock-echo"
    assert data["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Echo: hello there gateway",
    }
    assert data["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


async def test_stream_completion(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body(stream=True))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Echo: hello there gateway"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # usage is only forwarded when the client asks for it
    assert all("usage" not in c for c in chunks)


async def test_stream_include_usage(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body(stream=True, stream_options={"include_usage": True}))
    chunks = [json.loads(e) for e in parse_sse(r.text)[:-1]]
    assert chunks[-1]["usage"]["completion_tokens"] == 4


async def test_unknown_model_is_404_openai_error(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body(model="nope"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


async def test_validation_error_is_openai_shaped(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json={"model": "mock-echo", "messages": []})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_upstream_error_is_502(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body(model="mock-error"))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


async def test_upstream_error_before_stream_is_502(client: httpx.AsyncClient) -> None:
    r = await client.post(CHAT, json=body(model="mock-error", stream=True))
    assert r.status_code == 502
