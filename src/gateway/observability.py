"""Structured JSON logging with request ids, plus the ASGI middleware that ties requests,
logs and the latency histogram together.

Why a raw ASGI middleware instead of ``@app.middleware("http")``: it can watch the *last*
body message, so the measured latency of a streaming response covers the whole stream and
not just the time to send headers.
"""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.metrics import REQUEST_LATENCY

# ContextVars are per-task (and copied into child tasks), so concurrent requests never see
# each other's ids even though they share one thread.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_var: ContextVar[str | None] = ContextVar("tenant", default=None)

# Accept a caller-supplied id only if it is short and harmless (avoids log injection).
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Fields every LogRecord has; anything else passed via `extra=` is emitted as JSON.
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_QUIET_ROUTES = {"/metrics", "/healthz", "/readyz"}

logger = logging.getLogger("gateway.access")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "tenant": tenant_var.get(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Our middleware writes the access log; silence uvicorn's plain-text duplicate.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every upstream call at INFO; our own completion log line already covers it.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        tenant_token = tenant_var.set(None)
        start = time.perf_counter()
        status = 500  # if the app crashes before sending a response

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            # The router stores the matched route in the scope; use its template as label.
            route = getattr(scope.get("route"), "path", "unmatched")
            REQUEST_LATENCY.labels(scope["method"], route, str(status)).observe(duration)
            logger.log(
                logging.DEBUG if route in _QUIET_ROUTES else logging.INFO,
                "request",
                extra={
                    # Explicit (not only via contextvars) so the values survive if the record
                    # is formatted later, e.g. by a queue handler.
                    "request_id": request_id,
                    "tenant": tenant_var.get(),
                    "method": scope["method"],
                    "route": route,
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round(duration * 1000, 2),
                },
            )
            request_id_var.reset(rid_token)
            tenant_var.reset(tenant_token)
