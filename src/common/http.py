"""Small helpers for API Gateway proxy responses."""

import json
from decimal import Decimal
from typing import Any

# Never returned to API callers: the task token is a credential for resuming
# the workflow.
PRIVATE_ATTRS = ("pk", "sk", "review_task_token")


def _default(obj: Any):
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_default),
    }


def error(status: int, message: str) -> dict:
    return response(status, {"error": message})


def public_view(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in PRIVATE_ATTRS}


def json_body(event: dict) -> dict:
    raw = event.get("body") or "{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("request body is not valid JSON") from None
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body
