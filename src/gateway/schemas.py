"""OpenAI wire-format schemas (the subset the gateway needs).

``extra="allow"`` on requests means unknown OpenAI parameters (``seed``, ``tools`` ...) are kept
and forwarded upstream instead of rejected, so newer clients keep working.
"""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    # content may be null for assistant tool-call messages.
    content: str | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage | None = None


class Delta(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str | None = None
    content: str | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
    usage: Usage | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "llm-gateway"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
