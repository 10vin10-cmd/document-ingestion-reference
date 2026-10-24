"""POST /documents/{id}/review — a reviewer approves or rejects a document.

Body: {"approved": bool, "corrected_fields": {...}?, "reviewer": str}

Ordering matters. The record is moved out of PENDING_REVIEW first, with a
version check, and only then is the workflow resumed. If two reviewers submit
at once, exactly one conditional write wins; the other gets 409 and the
workflow is resumed once.
"""

from common import aws
from common.http import error, json_body, public_view, response
from common.repository import ConcurrencyError, DocumentNotFound, InvalidTransition
from common.states import Status


def handler(event, _context):
    document_id = (event.get("pathParameters") or {}).get("id", "")
    try:
        body = json_body(event)
    except ValueError as err:
        return error(400, str(err))

    approved = body.get("approved")
    reviewer = str(body.get("reviewer") or "").strip()
    if not isinstance(approved, bool) or not reviewer:
        return error(400, "'approved' (boolean) and 'reviewer' are required")
    corrections = body.get("corrected_fields") or {}
    if not isinstance(corrections, dict):
        return error(400, "'corrected_fields' must be an object")

    repo = aws.documents()
    try:
        item = repo.get(document_id)
    except DocumentNotFound:
        return error(404, "document not found")
    if item["status"] != Status.PENDING_REVIEW.value:
        return error(409, f"document is {item['status']}, not awaiting review")

    token = item["review_task_token"]
    set_attrs = {"reviewed_by": reviewer}
    if approved and corrections:
        fields = dict(item.get("fields", {}))
        for name, value in corrections.items():
            # A human-entered value is authoritative.
            fields[name] = {"value": str(value), "confidence": 100}
        set_attrs["fields"] = fields

    try:
        updated = repo.transition(
            document_id,
            Status.VALIDATED if approved else Status.REJECTED,
            int(item["version"]),
            set_attrs=set_attrs,
            remove_attrs=("review_task_token",),
        )
    except (ConcurrencyError, InvalidTransition):
        return error(409, "document was updated by someone else; reload and retry")

    aws.client("stepfunctions").send_task_success(
        taskToken=token, output='{"approved": %s}' % ("true" if approved else "false")
    )
    return response(200, public_view(updated))
