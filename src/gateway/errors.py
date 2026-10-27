"""OpenAI-style errors.

Every error leaves the gateway as ``{"error": {"message", "type", "param", "code"}}`` so the
official OpenAI SDKs raise the right exception class (AuthenticationError, RateLimitError ...).
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str,
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type_
        self.code = code
        self.param = param
        self.headers = headers

    def body(self) -> dict[str, object]:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }


def unauthorized(message: str = "Invalid or missing API key.") -> APIError:
    # 401 = "who are you?" (no/unknown credentials).
    return APIError(401, message, "invalid_request_error", "invalid_api_key")


def forbidden(message: str) -> APIError:
    # 403 = "I know who you are, but you may not do this."
    return APIError(403, message, "permission_error", "forbidden")


def rate_limited(retry_after: float) -> APIError:
    return APIError(
        429,
        "Rate limit exceeded. Retry later.",
        "rate_limit_error",
        "rate_limit_exceeded",
        headers={"Retry-After": str(max(1, round(retry_after)))},
    )


def quota_exceeded() -> APIError:
    # Same code OpenAI uses when a billing quota is exhausted.
    return APIError(
        429,
        "Monthly token quota exceeded.",
        "insufficient_quota",
        "insufficient_quota",
    )


def model_not_found(model: str) -> APIError:
    return APIError(
        404,
        f"The model '{model}' does not exist or is not deployed.",
        "invalid_request_error",
        "model_not_found",
        param="model",
    )


def upstream_error(message: str = "Upstream model backend failed.") -> APIError:
    return APIError(502, message, "api_error", "upstream_error")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(_: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        err = APIError(
            400,
            f"Invalid request: {first.get('msg', 'validation error')}",
            "invalid_request_error",
            "invalid_request",
            param=loc or None,
        )
        return JSONResponse(err.body(), status_code=400)
