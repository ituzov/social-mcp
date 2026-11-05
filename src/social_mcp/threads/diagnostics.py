"""Allowlisted publication metadata; never log request or response contents."""

import logging
from typing import Literal

type PublicationStage = Literal[
    "create", "status", "publish", "repost", "delete", "moderate"
]
type PublicationReason = Literal[
    "request_started",
    "response_received",
    "timeout",
    "network_error",
    "cancelled",
    "api_error",
    "invalid_json",
    "invalid_schema",
    "already_published",
    "not_ready",
    "confirmed",
]

logger = logging.getLogger(__name__)
_MISSING = object()
_STATES = {"IN_PROGRESS", "FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}


def _value_type(value: object) -> str:
    if value is _MISSING:
        return "missing"
    value_types: dict[type[object], str] = {
        type(None): "null",
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    return value_types.get(type(value), "other")


def record_publication(
    stage: PublicationStage,
    reason: PublicationReason,
    *,
    http_status: int | None = None,
    payload: object = None,
) -> None:
    """Log only fixed labels, an HTTP status, and safe response field metadata."""
    data = payload if type(payload) is dict else {}
    state = data.get("status", _MISSING)
    state_label = state if type(state) is str and state in _STATES else "unknown"
    status = (
        http_status if type(http_status) is int and 100 <= http_status <= 599 else None
    )
    level = (
        logging.INFO
        if reason in {"request_started", "response_received", "confirmed"}
        else logging.WARNING
    )
    logger.log(
        level,
        "Threads publication: stage=%s reason=%s http_status=%s id_type=%s container_status=%s",
        stage,
        reason,
        status,
        _value_type(data.get("id", _MISSING)),
        state_label,
    )
