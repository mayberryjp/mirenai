"""Error envelope helpers and request-body parsing."""

from __future__ import annotations

from typing import Any

from bottle import request, response
from pydantic import BaseModel, ValidationError


def error(code: str, message: str, status: int, detail: str | None = None) -> dict[str, Any]:
    response.status = status
    payload: dict[str, Any] = {"status": "error", "code": code, "error": message}
    if detail is not None:
        payload["detail"] = detail
    return payload


def _first_error_detail(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid request body"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    return f"{location}: {message}" if location else message


def parse_body(model: type[BaseModel]) -> tuple[BaseModel | None, dict[str, Any] | None]:
    """Parse and validate a JSON request body.

    Returns ``(instance, None)`` on success or ``(None, envelope)`` with the
    error response body already prepared and the HTTP status set.
    """
    try:
        raw = request.json
    except Exception:
        return None, error("bad_request", "Invalid request", 400, "request body is not valid JSON")
    if raw is None:
        return None, error(
            "bad_request", "Invalid request", 400, "request body must be a JSON object"
        )
    try:
        return model.model_validate(raw), None
    except ValidationError as exc:
        return None, error("validation_error", "Invalid request", 422, _first_error_detail(exc))


def read_pagination() -> tuple[bool, int | None, int, dict[str, Any] | None]:
    """Interpret optional ``limit``/``offset`` query parameters.

    Returns ``(paginate, limit, offset, error)``. When both parameters are
    absent, ``paginate`` is ``False`` and the caller must return every row.
    """
    raw_limit = request.query.get("limit")
    raw_offset = request.query.get("offset")
    if raw_limit is None and raw_offset is None:
        return False, None, 0, None
    try:
        limit = int(raw_limit) if raw_limit is not None else None
        offset = int(raw_offset) if raw_offset is not None else 0
    except ValueError:
        return False, None, 0, error(
            "validation_error", "Invalid request", 422, "limit and offset must be integers"
        )
    return True, limit, offset, None
