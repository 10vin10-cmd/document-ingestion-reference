import json

from handlers import create_document, get_document


def _post(body, headers=None):
    return create_document.handler(
        {"body": json.dumps(body), "headers": headers or {}}, None)


def test_create_returns_upload_url(aws):
    resp = _post({"business_queue": "retail", "file_name": "inv 1.pdf"})
    body = json.loads(resp["body"])
    assert resp["statusCode"] == 201
    assert body["status"] == "PENDING_UPLOAD"
    assert body["s3_key"].endswith("/inv_1.pdf")  # file name sanitised
    assert "X-Amz-Signature" in body["upload_url"]


def test_idempotency_key_returns_same_document(aws):
    headers = {"Idempotency-Key": "client-req-42"}
    first = json.loads(_post({"business_queue": "retail"}, headers)["body"])
    retry = _post({"business_queue": "retail"}, headers)
    assert retry["statusCode"] == 200
    assert json.loads(retry["body"])["document_id"] == first["document_id"]


def test_rejects_unknown_queue(aws):
    assert _post({"business_queue": "unknown"})["statusCode"] == 400


def test_rejects_malformed_hash(aws):
    resp = _post({"business_queue": "retail", "content_sha256": "abc"})
    assert resp["statusCode"] == 400


def test_rejects_non_json_body(aws):
    resp = create_document.handler({"body": "not json"}, None)
    assert resp["statusCode"] == 400


def test_get_missing_document_is_404(aws):
    resp = get_document.handler({"pathParameters": {"id": "nope"}}, None)
    assert resp["statusCode"] == 404


def test_get_never_exposes_task_token(aws):
    repo = aws["repo"]
    repo.create("d1", "retail", "incoming/d1/x.pdf")
    repo._table.update_item(  # simulate a parked document
        Key={"pk": "DOC#d1", "sk": "META"},
        UpdateExpression="SET review_task_token = :t",
        ExpressionAttributeValues={":t": "secret"},
    )
    body = json.loads(get_document.handler({"pathParameters": {"id": "d1"}}, None)["body"])
    assert "review_task_token" not in body
    assert "pk" not in body
