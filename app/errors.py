"""Unified error model for every v2 endpoint.

Success and failure envelopes are intentionally flat:

    {"success": true,  ...}
    {"success": false, "error_id": "...", "error_message": "..."}

The v1 passthrough is the only exception: it returns the upstream response verbatim.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ForgeError(Exception):
    """Domain error raised by Forge."""

    def __init__(
        self,
        error_id: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.error_id = error_id
        self.message = message
        self.http_status = http_status
        self.details = details


# Strings longer than this in an error envelope are replaced wholesale: an
# error must state the reason, never echo the caller's (or upstream's) raw text.
_ERROR_STRING_LIMIT = 500


def redact_error_text(value: Any, limit: int = _ERROR_STRING_LIMIT) -> Any:
    """Recursively replace over-long strings with ``<omitted N characters>``."""
    if isinstance(value, str):
        return value if len(value) <= limit else f"<omitted {len(value)} characters>"
    if isinstance(value, dict):
        return {k: redact_error_text(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_error_text(v, limit) for v in value]
    return value


def error_body(error_id: str, message: str, details: Any = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "success": False,
        "error_id": error_id,
        "error_message": redact_error_text(message),
    }
    if details is not None:
        body["details"] = redact_error_text(details)
    return body


def error_response(
    error_id: str,
    message: str,
    http_status: int = status.HTTP_400_BAD_REQUEST,
    details: Any = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status, content=error_body(error_id, message, details)
    )


# HTTP 400 - the caller sent something unusable
def bad_request(
    message: str, error_id: str = "BAD_REQUEST", details: Any = None
) -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_400_BAD_REQUEST, details)


# HTTP 401 - could not establish who is calling
def unauthorized(
    message: str = "Verification failed", error_id: str = "UNAUTHORIZED"
) -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_401_UNAUTHORIZED)


def invalid_signature(message: str = "Invalid signature") -> ForgeError:
    return ForgeError("INVALID_SIGNATURE", message, status.HTTP_401_UNAUTHORIZED)


def invalid_api_key(message: str = "WeKnora API key is invalid") -> ForgeError:
    return ForgeError("INVALID_API_KEY", message, status.HTTP_401_UNAUTHORIZED)


# HTTP 403 - identity is known but not allowed
def forbidden(
    message: str = "Permission denied", error_id: str = "FORBIDDEN"
) -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_403_FORBIDDEN)


# HTTP 502 / 504 - WeKnora or PostgreSQL misbehaved
def upstream_error(
    message: str, details: Any = None, http_status: int = status.HTTP_502_BAD_GATEWAY
) -> ForgeError:
    return ForgeError("UPSTREAM_ERROR", message, http_status, details)


def database_error(message: str, details: Any = None) -> ForgeError:
    return ForgeError("DATABASE_ERROR", message, status.HTTP_502_BAD_GATEWAY, details)


# Length-check error types where the echoed input is worth summarising as a count
_LENGTH_ERRORS = {"string_too_long", "string_too_short"}


def sanitize_validation_errors(errors: list) -> list:
    """Make pydantic validation errors safe to return to the caller.

    - drop ``input`` entirely: it echoes the raw request body (for a 10k-char
      content that doubles the response and leaks the article back in logs)
    - for length errors append the actual length so the caller can tell how
      far over the limit they are without counting by hand
    """
    safe: list = []
    for err in errors:
        e = dict(err)
        raw = e.pop("input", None)
        if e.get("type") in _LENGTH_ERRORS and isinstance(raw, str):
            actual = len(raw)
            if isinstance(e.get("ctx"), dict):
                e["ctx"] = {**e["ctx"], "actual_length": actual}
            e["msg"] = f"{e.get('msg', '')} (actual: {actual})"
        safe.append(e)
    return safe


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ForgeError)
    async def _forge_error_handler(request: Request, exc: ForgeError) -> JSONResponse:
        return error_response(exc.error_id, exc.message, exc.http_status, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            "INVALID_REQUEST",
            "Request validation failed",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            sanitize_validation_errors(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return error_response(
            "INTERNAL_ERROR",
            "Internal server error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            str(exc),
        )
