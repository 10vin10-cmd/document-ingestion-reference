"""Reviewer API: authorization, the review queue, claims and decisions."""

import json
from datetime import timedelta

import boto3
import pytest
from conftest import api_event

from handlers import reviews, workflow

PDF = b"%PDF-1.7\n" + b"0" * 100


def _call(*args, **kwargs):
    resp = reviews.handler(api_event(*args, **kwargs), None)
    body = json.loads(resp["body"]) if resp["body"] not in (None, "null") else None
    return resp["statusCode"], body


def _park(aws, doc_id="d1", queue="corporate", token="tok"):
    """Run a document through the workflow until it waits for review. The
    stub extractor reads the tax id at 88%, below corporate's 92% bar."""
    key = f"incoming/{doc_id}/invoice.pdf"
    aws["repo"].create(doc_id, queue, key)
    aws["repo"].transition(doc_id, workflow.Status.RECEIVED, 1)
    boto3.client("s3").put_object(Bucket="documents-bucket", Key=key, Body=PDF)
    state = {"document_id": doc_id, "bucket": "documents-bucket", "key": key}
    state = workflow.validate(workflow.extract(workflow.scan(state, None), None), None)
    assert state["needs_review"]
    workflow.request_review({**state, "task_token": token}, None)


# -- authorization -------------------------------------------------------------

def test_unauthenticated_request_is_forbidden(aws):
    status, _ = _call("GET", "/reviews", user=None)
    assert status == 403


def test_user_outside_reviewers_group_is_forbidden(aws):
    status, body = _call("GET", "/reviews", groups="finance-viewers")
    assert status == 403
    assert "reviewers" in body["error"]


@pytest.mark.parametrize("groups", ["reviewers", "admins,reviewers", "[admins, reviewers]"])
def test_group_claim_formats_are_understood(aws, groups):
    assert _call("GET", "/reviews", groups=groups)[0] == 200


# -- queue -------------------------------------------------------------------

def test_queue_lists_only_documents_awaiting_review(aws):
    _park(aws, "d1")
    aws["repo"].create("d2", "corporate", "incoming/d2/x.pdf")  # not in review
    status, body = _call("GET", "/reviews")
    assert status == 200
    assert [i["document_id"] for i in body["items"]] == ["d1"]
    assert body["items"][0]["review_issues"] == ["low_confidence:TAX_PAYER_ID"]


def test_queue_never_exposes_task_tokens(aws):
    _park(aws)
    _, body = _call("GET", "/reviews")
    assert "review_task_token" not in json.dumps(body)


def test_queue_can_be_filtered(aws):
    _park(aws, "d1", queue="corporate")
    assert _call("GET", "/reviews", query={"queue": "retail"})[1]["items"] == []
    assert _call("GET", "/reviews", query={"queue": "nope"})[0] == 400


# -- claims ------------------------------------------------------------------

def test_claim_returns_document_with_preview_and_template(aws):
    _park(aws)
    status, body = _call("POST", "/reviews/{id}/claim", "d1")
    assert status == 200
    assert body["claimed_by"] == "asha"
    assert body["template"]["required_fields"][-1] == "TAX_PAYER_ID"
    assert "review_task_token" not in body
    url = body["preview_url"]
    assert "documents-bucket.s3.ap-south-1.amazonaws.com" in url  # regional endpoint
    assert "response-content-type=application%2Fpdf" in url
    assert "response-content-disposition=inline" in url


def test_second_reviewer_cannot_claim_a_held_document(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1", user="asha")
    status, body = _call("POST", "/reviews/{id}/claim", "d1", user="ravi")
    assert status == 409
    assert body["claimed_by"] == "asha"


def test_claiming_again_renews_your_own_lease(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1")
    assert _call("POST", "/reviews/{id}/claim", "d1")[0] == 200


def test_expired_claim_can_be_taken_over(aws):
    _park(aws)
    aws["repo"].claim("d1", "asha", timedelta(seconds=-1))  # already expired
    status, body = _call("POST", "/reviews/{id}/claim", "d1", user="ravi")
    assert status == 200
    assert body["claimed_by"] == "ravi"


def test_release_frees_the_document(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1", user="asha")
    assert _call("POST", "/reviews/{id}/release", "d1", user="asha")[0] == 204
    assert _call("POST", "/reviews/{id}/claim", "d1", user="ravi")[0] == 200


def test_cannot_release_someone_elses_claim(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1", user="asha")
    _call("POST", "/reviews/{id}/release", "d1", user="ravi")
    assert aws["repo"].get("d1")["claimed_by"] == "asha"


def test_read_only_view_works_while_someone_else_holds_it(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1", user="asha")
    status, body = _call("GET", "/reviews/{id}", "d1", user="ravi")
    assert status == 200
    assert body["claimed_by"] == "asha"


def test_claiming_a_document_not_in_review_is_rejected(aws):
    aws["repo"].create("d1", "retail", "incoming/d1/x.pdf")
    assert _call("POST", "/reviews/{id}/claim", "d1")[0] == 409
    assert _call("POST", "/reviews/{id}/claim", "missing")[0] == 404


# -- decisions ---------------------------------------------------------------

def test_approval_records_corrections_with_previous_values(aws):
    _park(aws)
    status, body = _call("POST", "/reviews/{id}", "d1", {
        "approved": True,
        "corrected_fields": {"TAX_PAYER_ID": "36ABCDE1234F1Z5", "VENDOR_NAME":
                             "Example Supplies Pvt Ltd"},  # unchanged: not recorded
    })
    assert status == 200
    assert body["status"] == "VALIDATED"
    assert body["review_corrections"] == {
        "TAX_PAYER_ID": {"previous": "36AAAAA0000A1Z5", "corrected": "36ABCDE1234F1Z5"}}
    assert body["reviewed_by"] == "asha"  # from the token, not the request body
    assert aws["sfn"].task_success[0]["output"] == '{"approved": true}'


def test_approval_requires_every_required_field(aws):
    _park(aws)
    status, body = _call("POST", "/reviews/{id}", "d1", {
        "approved": True, "corrected_fields": {"VENDOR_NAME": "  "}})
    assert status == 400
    assert "VENDOR_NAME" in body["error"]
    assert aws["sfn"].task_success == []


def test_unknown_fields_cannot_be_injected(aws):
    _park(aws)
    status, _ = _call("POST", "/reviews/{id}", "d1", {
        "approved": True, "corrected_fields": {"status": "COMPLETED"}})
    assert status == 400


def test_rejection_requires_a_reason(aws):
    _park(aws)
    assert _call("POST", "/reviews/{id}", "d1", {"approved": False})[0] == 400
    status, body = _call("POST", "/reviews/{id}", "d1",
                         {"approved": False, "reason": "Duplicate of INV-1001"})
    assert status == 200
    assert body["status"] == "REJECTED"
    assert body["review_reason"] == "Duplicate of INV-1001"


def test_cannot_decide_while_another_reviewer_holds_the_claim(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1", user="asha")
    status, body = _call("POST", "/reviews/{id}", "d1", {"approved": True}, user="ravi")
    assert status == 409
    assert body["claimed_by"] == "asha"
    assert aws["sfn"].task_success == []


def test_decision_clears_all_review_state(aws):
    _park(aws)
    _call("POST", "/reviews/{id}/claim", "d1")
    _call("POST", "/reviews/{id}", "d1", {"approved": True})
    item = aws["repo"].get("d1")
    for attr in ("review_queue", "review_task_token", "claimed_by", "claim_expires_at"):
        assert attr not in item
    assert _call("GET", "/reviews")[1]["items"] == []


def test_workflow_is_resumed_exactly_once(aws):
    _park(aws)
    assert _call("POST", "/reviews/{id}", "d1", {"approved": True})[0] == 200
    assert _call("POST", "/reviews/{id}", "d1", {"approved": True})[0] == 409
    assert len(aws["sfn"].task_success) == 1


def test_bad_json_is_rejected(aws):
    _park(aws)
    event = api_event("POST", "/reviews/{id}", "d1")
    event["body"] = "{not json"
    assert reviews.handler(event, None)["statusCode"] == 400
