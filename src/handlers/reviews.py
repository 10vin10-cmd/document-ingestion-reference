"""Reviewer API, used by the web app. Every route requires a Cognito user in
the ``reviewers`` group.

  GET  /reviews?queue=...        documents waiting for review, oldest first
  GET  /reviews/{id}             read-only view (for documents someone else holds)
  POST /reviews/{id}/claim       take a lease and return the document to work on
  POST /reviews/{id}/release     give the lease back
  POST /reviews/{id}             submit the decision

Submitting a decision moves the record out of PENDING_REVIEW with a version-
and claim-checked write *first*, then resumes the workflow. If two reviewers
race, exactly one write wins and the workflow resumes once.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

from common import aws
from common.http import error, json_body, public_view, response
from common.identity import Forbidden, Reviewer, reviewer_from_event
from common.repository import (
    ClaimConflict,
    ConcurrencyError,
    DocumentNotFound,
    InvalidTransition,
)
from common.states import REVIEW_ATTRS, Status
from common.templates import TEMPLATES, get_template

log = logging.getLogger()
log.setLevel(logging.INFO)

CLAIM_TTL = timedelta(minutes=15)
PREVIEW_URL_TTL_SECONDS = 300
MAX_FIELD_LENGTH = 256
MAX_REASON_LENGTH = 1000
_LIST_ATTRS = ("document_id", "business_queue", "created_at", "review_issues",
               "possible_duplicate_of", "claimed_by", "claim_expires_at")


def handler(event, _context):
    try:
        reviewer = reviewer_from_event(event)
    except Forbidden as err:
        return error(403, str(err))

    method = event.get("httpMethod")
    resource = event.get("resource")
    document_id = (event.get("pathParameters") or {}).get("id", "")

    try:
        if method == "GET" and resource == "/reviews":
            return _list(event)
        if method == "GET" and resource == "/reviews/{id}":
            return _view(document_id)
        if method == "POST" and resource == "/reviews/{id}/claim":
            return _claim(document_id, reviewer)
        if method == "POST" and resource == "/reviews/{id}/release":
            return _release(document_id, reviewer)
        if method == "POST" and resource == "/reviews/{id}":
            return _submit(document_id, reviewer, event)
    except DocumentNotFound:
        return error(404, "document not found")
    return error(405, f"{method} {resource} is not supported")


# -- routes ------------------------------------------------------------------

def _list(event):
    queue = (event.get("queryStringParameters") or {}).get("queue")
    if queue and queue not in TEMPLATES:
        return error(400, f"queue must be one of {sorted(TEMPLATES)}")
    repo = aws.documents()
    items = []
    for q in [queue] if queue else sorted(TEMPLATES):
        items.extend(repo.list_pending_review(q))
    items.sort(key=lambda i: i["created_at"])
    return response(200, {"items": [{k: i[k] for k in _LIST_ATTRS if k in i}
                                    for i in items]})


def _view(document_id):
    item = aws.documents().get(document_id)
    if item["status"] != Status.PENDING_REVIEW.value:
        return error(409, f"document is {item['status']}, not awaiting review")
    return response(200, _detail(item))


def _claim(document_id, reviewer: Reviewer):
    try:
        item = aws.documents().claim(document_id, reviewer.username, CLAIM_TTL)
    except ClaimConflict as err:
        return response(409, {"error": "claimed by another reviewer",
                              "claimed_by": err.holder})
    except InvalidTransition as err:
        return error(409, str(err))
    return response(200, _detail(item))


def _release(document_id, reviewer: Reviewer):
    aws.documents().release_claim(document_id, reviewer.username)
    return response(204, None)


def _submit(document_id, reviewer: Reviewer, event):
    try:
        decision = _parse_decision(json_body(event))
    except ValueError as err:
        return error(400, str(err))

    repo = aws.documents()
    item = repo.get(document_id)
    if item["status"] != Status.PENDING_REVIEW.value:
        return error(409, f"document is {item['status']}, not awaiting review")

    set_attrs = {"reviewed_by": reviewer.username}
    if decision["approved"]:
        try:
            fields, corrections = _apply_corrections(item, decision["corrected_fields"])
        except ValueError as err:
            return error(400, str(err))
        set_attrs["fields"] = fields
        if corrections:
            set_attrs["review_corrections"] = corrections
    else:
        set_attrs["review_reason"] = decision["reason"]

    token = item["review_task_token"]
    try:
        updated = repo.transition(
            document_id,
            Status.VALIDATED if decision["approved"] else Status.REJECTED,
            int(item["version"]),
            set_attrs=set_attrs,
            remove_attrs=REVIEW_ATTRS,
            claimable_by=reviewer.username,
        )
    except ClaimConflict as err:
        return response(409, {"error": "claimed by another reviewer",
                              "claimed_by": err.holder})
    except (ConcurrencyError, InvalidTransition):
        return error(409, "document was updated by someone else; reload and retry")

    aws.client("stepfunctions").send_task_success(
        taskToken=token, output=json.dumps({"approved": decision["approved"]}))
    log.info("%s %s by %s", document_id, updated["status"], reviewer.username)
    return response(200, public_view(updated))


# -- helpers -----------------------------------------------------------------

def _parse_decision(body: dict) -> dict:
    approved = body.get("approved")
    if not isinstance(approved, bool):
        raise ValueError("'approved' (boolean) is required")
    if approved:
        corrections = body.get("corrected_fields") or {}
        if not isinstance(corrections, dict):
            raise ValueError("'corrected_fields' must be an object")
        return {"approved": True, "corrected_fields": corrections}
    reason = str(body.get("reason") or "").strip()
    if not reason:
        raise ValueError("a reason is required to reject a document")
    return {"approved": False, "reason": reason[:MAX_REASON_LENGTH]}


def _apply_corrections(item: dict, corrections: dict) -> tuple[dict, dict]:
    """Merge reviewer edits into the extracted fields. Each change is kept with
    its previous value for audit. Approval requires every required field."""
    template = get_template(item["business_queue"])
    fields = dict(item.get("fields", {}))
    allowed = set(template.required_fields) | set(fields)
    audit = {}
    for name, raw in corrections.items():
        if name not in allowed:
            raise ValueError(f"unknown field: {name}")
        value = str(raw).strip()
        if len(value) > MAX_FIELD_LENGTH:
            raise ValueError(f"{name} is longer than {MAX_FIELD_LENGTH} characters")
        previous = fields.get(name, {}).get("value")
        if value == previous:
            continue
        # A human-entered value is authoritative.
        fields[name] = {"value": value, "confidence": 100}
        audit[name] = {"previous": previous, "corrected": value}

    missing = [f for f in template.required_fields
               if not str(fields.get(f, {}).get("value", "")).strip()]
    if missing:
        raise ValueError(f"required fields are empty: {', '.join(missing)}")
    return fields, audit


def _detail(item: dict) -> dict:
    template = get_template(item["business_queue"])
    body = public_view(item)
    body["template"] = {"required_fields": list(template.required_fields),
                        "min_confidence": template.min_confidence}
    body["preview_url"] = aws.client("s3").generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["BUCKET_NAME"],
            "Key": item["s3_key"],
            # Uploads carry no reliable content type; serve what the scan
            # step detected so the browser can display it inline.
            "ResponseContentType": item.get("content_type", "application/octet-stream"),
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=PREVIEW_URL_TTL_SECONDS,
    )
    return body
