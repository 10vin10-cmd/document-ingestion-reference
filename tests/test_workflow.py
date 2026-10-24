"""End-to-end through the handlers, with Step Functions replaced by direct
calls in the order the state machine makes them."""

import json

import boto3

from handlers import on_object_created, submit_review, workflow

PDF = b"%PDF-1.7\n" + b"0" * 100


def _upload(aws, queue="retail", doc_id="d1", data=PDF):
    key = f"incoming/{doc_id}/invoice.pdf"
    aws["repo"].create(doc_id, queue, key)
    boto3.client("s3").put_object(Bucket="documents-bucket", Key=key, Body=data)
    event = {"Records": [{"s3": {"bucket": {"name": "documents-bucket"},
                                 "object": {"key": key}}}]}
    on_object_created.handler(event, None)
    return {"document_id": doc_id, "bucket": "documents-bucket", "key": key}


def _review(doc_id, approved=True, **extra):
    body = {"approved": approved, "reviewer": "finops.user", **extra}
    return submit_review.handler(
        {"pathParameters": {"id": doc_id}, "body": json.dumps(body)}, None)


def test_upload_starts_one_workflow_even_if_event_is_redelivered(aws):
    _upload(aws)
    on_object_created.handler({"Records": [{"s3": {
        "bucket": {"name": "documents-bucket"},
        "object": {"key": "incoming/d1/invoice.pdf"}}}]}, None)
    assert len(aws["sfn"].started) == 1
    assert aws["sfn"].started[0]["name"] == "d1"
    assert aws["repo"].get("d1")["status"] == "RECEIVED"


def test_clean_confident_document_completes_without_review(aws):
    state = _upload(aws, queue="retail")
    state = workflow.scan(state, None)
    assert state["clean"]
    state = workflow.extract(state, None)
    state = workflow.validate(state, None)
    assert state["needs_review"] is False
    workflow.finalize(state, None)
    assert aws["repo"].get("d1")["status"] == "COMPLETED"


def test_unsupported_file_is_quarantined(aws):
    state = workflow.scan(_upload(aws, data=b"MZ\x90\x00 not a document"), None)
    assert state["clean"] is False
    item = aws["repo"].get("d1")
    assert item["status"] == "QUARANTINED"
    assert item["quarantine_reason"] == "unsupported file type"


def test_low_confidence_document_waits_for_review_then_completes(aws):
    # The stub reads the tax id at 88% confidence; corporate requires 92%.
    state = workflow.validate(workflow.extract(workflow.scan(
        _upload(aws, queue="corporate"), None), None), None)
    assert state["needs_review"]
    assert "low_confidence:TAX_PAYER_ID" in state["issues"]

    workflow.request_review({**state, "task_token": "tok-1"}, None)
    assert aws["repo"].get("d1")["status"] == "PENDING_REVIEW"

    resp = _review("d1", corrected_fields={"TAX_PAYER_ID": "36AAAAA0000A1Z5"})
    assert resp["statusCode"] == 200
    item = aws["repo"].get("d1")
    assert item["status"] == "VALIDATED"
    assert item["fields"]["TAX_PAYER_ID"]["confidence"] == 100
    assert "review_task_token" not in item
    assert aws["sfn"].task_success == [{"taskToken": "tok-1",
                                        "output": '{"approved": true}'}]


def test_second_review_submission_is_rejected(aws):
    state = workflow.validate(workflow.extract(workflow.scan(
        _upload(aws, queue="corporate"), None), None), None)
    workflow.request_review({**state, "task_token": "tok-1"}, None)

    assert _review("d1")["statusCode"] == 200
    assert _review("d1", approved=False)["statusCode"] == 409
    assert len(aws["sfn"].task_success) == 1  # workflow resumed exactly once


def test_rejection_is_terminal(aws):
    state = workflow.validate(workflow.extract(workflow.scan(
        _upload(aws, queue="corporate"), None), None), None)
    workflow.request_review({**state, "task_token": "tok-1"}, None)
    _review("d1", approved=False)
    assert aws["repo"].get("d1")["status"] == "REJECTED"


def test_steps_are_safe_to_retry(aws):
    state = workflow.scan(_upload(aws), None)
    again = workflow.scan(state, None)  # e.g. Lambda response lost, SFN retries
    assert again["clean"]
    workflow.extract(state, None)
    workflow.extract(state, None)
    assert aws["repo"].get("d1")["version"] == 4  # created, received, scanned, extracted


def test_failure_handler_does_not_overwrite_terminal_state(aws):
    state = workflow.scan(_upload(aws, data=b"junk"), None)  # quarantined
    workflow.mark_failed({**state, "error": {"Error": "Boom"}}, None)
    assert aws["repo"].get("d1")["status"] == "QUARANTINED"


def test_failure_handler_records_cause(aws):
    state = _upload(aws)
    workflow.mark_failed({**state, "error": {"Error": "States.Timeout",
                                             "Cause": "extract timed out"}}, None)
    item = aws["repo"].get("d1")
    assert item["status"] == "FAILED"
    assert item["failure"]["error"] == "States.Timeout"
