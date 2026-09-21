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


def error_body(error_id: str, message: str, details: Any = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "success": False,
        "error_id": error_id,
        "error_message": message,
    }
    if details is not None:
        body["details"] = details
    return body


def error_response(
    error_id: str,
    message: str,
    http_status: int = status.HTTP_400_BAD_REQUEST,
    details: Any = None,
) -> JSONResponse:
    return JSONResponse(status_code=http_status, content=error_body(error_id, message, details))


# HTTP 400 - the caller sent something unusable
def bad_request(message: str, error_id: str = "BAD_REQUEST", details: Any = None) -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_400_BAD_REQUEST, details)


# HTTP 401 - could not establish who is calling
def unauthorized(message: str = "Verification failed", error_id: str = "UNAUTHORIZED") -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_401_UNAUTHORIZED)


def invalid_signature(message: str = "Invalid signature") -> ForgeError:
    return ForgeError("INVALID_SIGNATURE", message, status.HTTP_401_UNAUTHORIZED)


def invalid_api_key(message: str = "WeKnora API key is invalid") -> ForgeError:
    return ForgeError("INVALID_API_KEY", message, status.HTTP_401_UNAUTHORIZED)


# HTTP 403 - identity is known but not allowed
def forbidden(message: str = "Permission denied", error_id: str = "FORBIDDEN") -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_403_FORBIDDEN)


# HTTP 404
def not_found(message: str = "Resource not found", error_id: str = "NOT_FOUND") -> ForgeError:
    return ForgeError(error_id, message, status.HTTP_404_NOT_FOUND)


# HTTP 502 / 504 - WeKnora or PostgreSQL misbehaved
def upstream_error(message: str, details: Any = None, http_status: int = status.HTTP_502_BAD_GATEWAY) -> ForgeError:
    return ForgeError("UPSTREAM_ERROR", message, http_status, details)


def database_error(message: str, details: Any = None) -> ForgeError:
    return ForgeError("DATABASE_ERROR", message, status.HTTP_502_BAD_GATEWAY, details)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ForgeError)
    async def _forge_error_handler(request: Request, exc: ForgeError) -> JSONResponse:
        return error_response(exc.error_id, exc.message, exc.http_status, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            "INVALID_REQUEST",
            "Request validation failed",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return error_response(
            "INTERNAL_ERROR",
            "Internal server error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            str(exc),
        )
