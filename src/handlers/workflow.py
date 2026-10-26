"""Task handlers invoked by the Step Functions workflow.

Each step re-reads the record and advances it with a version-checked
transition. Steps are written to be safe under Step Functions retries: if a
step already completed (its Lambda succeeded but the response was lost), the
retry sees the target status and returns the stored result instead of
failing or redoing the work.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal

from common import aws
from common.repository import InvalidTransition
from common.states import REVIEW_ATTRS, TERMINAL, Status
from common.templates import get_template
from common.validation import assess

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_BYTES = 20 * 1024 * 1024
# Leading bytes of the file types the pipeline accepts.
_MAGIC = {
    b"%PDF": "application/pdf",
    b"\x89PNG": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
}


# -- 1. screening ------------------------------------------------------------

def scan(event, _context):
    doc_id, bucket, key = event["document_id"], event["bucket"], event["key"]
    repo = aws.documents()
    item = repo.get(doc_id)
    if item["status"] in (Status.SCANNED.value, Status.QUARANTINED.value):
        return {**event, "clean": item["status"] == Status.SCANNED.value}

    s3 = aws.client("s3")
    head = s3.head_object(Bucket=bucket, Key=key)
    reason = None
    if head["ContentLength"] > MAX_BYTES:
        reason = "file too large"
    else:
        start = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-7")["Body"].read()
        content_type = next((t for m, t in _MAGIC.items() if start.startswith(m)), None)
        if content_type is None:
            reason = "unsupported file type"
    # A real deployment calls a malware engine here (for example an
    # S3 malware-protection plan or a scanning service) before accepting.

    if reason:
        repo.transition(doc_id, Status.QUARANTINED, int(item["version"]),
                        set_attrs={"quarantine_reason": reason})
        return {**event, "clean": False, "reason": reason}
    repo.transition(doc_id, Status.SCANNED, int(item["version"]),
                    set_attrs={"content_type": content_type})
    return {**event, "clean": True}


# -- 2. extraction -----------------------------------------------------------

def extract(event, _context):
    doc_id = event["document_id"]
    repo = aws.documents()
    item = repo.get(doc_id)
    if item["status"] != Status.SCANNED.value:
        return {**event}  # already extracted on a previous attempt

    if os.environ.get("EXTRACTOR", "textract") == "stub":
        fields = _stub_fields()
    else:
        fields = _textract_fields(event["bucket"], event["key"])

    repo.transition(doc_id, Status.EXTRACTED, int(item["version"]),
                    set_attrs={"fields": _to_dynamo(fields)})
    return {**event}


def _textract_fields(bucket: str, key: str) -> dict[str, dict]:
    """Synchronous AnalyzeExpense handles images and single-page PDFs. For
    multi-page documents, switch to StartExpenseAnalysis and resume the
    workflow from the completion notification."""
    resp = aws.client("textract").analyze_expense(
        Document={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    fields: dict[str, dict] = {}
    for doc in resp.get("ExpenseDocuments", []):
        for f in doc.get("SummaryFields", []):
            ftype = f.get("Type", {}).get("Text")
            value = f.get("ValueDetection", {})
            if not ftype or "Text" not in value:
                continue
            confidence = float(value.get("Confidence", 0))
            # Keep the most confident reading when a field appears twice.
            if ftype not in fields or confidence > fields[ftype]["confidence"]:
                fields[ftype] = {"value": value["Text"], "confidence": confidence}
    return fields


def _stub_fields() -> dict[str, dict]:
    """Deterministic output for demos and tests without Textract costs."""
    return {
        "INVOICE_RECEIPT_ID": {"value": "INV-1001", "confidence": 99.1},
        "INVOICE_RECEIPT_DATE": {"value": "2026-10-01", "confidence": 97.4},
        "VENDOR_NAME": {"value": "Example Supplies Pvt Ltd", "confidence": 98.0},
        "TOTAL": {"value": "12,450.00", "confidence": 96.2},
        "TAX_PAYER_ID": {"value": "36AAAAA0000A1Z5", "confidence": 88.0},
    }


# -- 3. validation -----------------------------------------------------------

def validate(event, _context):
    doc_id = event["document_id"]
    repo = aws.documents()
    item = repo.get(doc_id)
    if item["status"] == Status.VALIDATED.value:
        return {**event, "needs_review": False, "issues": []}

    result = assess(_from_dynamo(item.get("fields", {})),
                    get_template(item["business_queue"]))
    if not result.needs_review:
        repo.transition(doc_id, Status.VALIDATED, int(item["version"]))
    return {**event, "needs_review": result.needs_review, "issues": result.issues}


# -- 4. human review (waitForTaskToken) -------------------------------------

def request_review(event, _context):
    """Park the document for a reviewer. The workflow pauses until the review
    API returns the task token to Step Functions."""
    doc_id = event["document_id"]
    repo = aws.documents()
    item = repo.get(doc_id)
    repo.transition(
        doc_id,
        Status.PENDING_REVIEW,
        int(item["version"]),
        set_attrs={
            "review_task_token": event["task_token"],
            "review_issues": event.get("issues", []),
            # Puts the document into the sparse review-queue index.
            "review_queue": item["business_queue"],
        },
    )
    return {"parked": True}


# -- 5. completion -----------------------------------------------------------

def finalize(event, _context):
    doc_id = event["document_id"]
    repo = aws.documents()
    item = repo.get(doc_id)
    if item["status"] != Status.COMPLETED.value:
        item = repo.transition(doc_id, Status.COMPLETED, int(item["version"]))

    # Downstream consumers (payables, notifications) subscribe to this event
    # instead of being called directly, so adding one needs no change here.
    aws.client("events").put_events(Entries=[{
        "Source": "document-ingestion",
        "DetailType": "DocumentCompleted",
        "Detail": json.dumps({
            "document_id": doc_id,
            "business_queue": item["business_queue"],
        }),
        "EventBusName": os.environ.get("EVENT_BUS_NAME", "default"),
    }])
    return {**event, "status": Status.COMPLETED.value}


def mark_failed(event, _context):
    """Catch-all for unrecoverable errors in any step."""
    doc_id = event["document_id"]
    repo = aws.documents()
    item = repo.get(doc_id)
    if Status(item["status"]) in TERMINAL:
        return {**event}
    error = event.get("error", {})
    try:
        repo.transition(doc_id, Status.FAILED, int(item["version"]), set_attrs={
            "failure": {
                "error": str(error.get("Error", "Unknown"))[:256],
                "cause": str(error.get("Cause", ""))[:1024],
            }
        }, remove_attrs=REVIEW_ATTRS)  # e.g. review timed out: leave the queue
    except InvalidTransition:
        log.warning("%s reached a terminal state concurrently", doc_id)
    return {**event}


# -- helpers -----------------------------------------------------------------

def _to_dynamo(fields: dict[str, dict]) -> dict:
    """DynamoDB rejects floats; store confidences as Decimal."""
    return {
        k: {"value": v["value"], "confidence": Decimal(str(round(v["confidence"], 2)))}
        for k, v in fields.items()
    }


def _from_dynamo(fields: dict) -> dict[str, dict]:
    return {
        k: {"value": v["value"], "confidence": float(v["confidence"])}
        for k, v in fields.items()
    }
