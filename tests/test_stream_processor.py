from boto3.dynamodb.types import TypeSerializer

from handlers import stream_processor

_ser = TypeSerializer()


def _image(**attrs):
    return {k: _ser.serialize(v) for k, v in attrs.items()}


def _record(seq, old, new, event="MODIFY"):
    return {
        "eventName": event,
        "dynamodb": {
            "SequenceNumber": seq,
            "ApproximateCreationDateTime": 1761300000,
            "OldImage": _image(**old) if old else None,
            "NewImage": _image(**new) if new else None,
        },
    }


def test_status_change_is_written_to_audit_table(aws):
    base = {"sk": "META", "document_id": "d1", "updated_at": "t"}
    resp = stream_processor.handler({"Records": [
        _record("100", None, {**base, "status": "PENDING_UPLOAD", "version": 1}, "INSERT"),
        _record("101", {**base, "status": "PENDING_UPLOAD", "version": 1},
                {**base, "status": "RECEIVED", "version": 2}),
    ]}, None)
    assert resp == {"batchItemFailures": []}

    items = aws["ddb"].Table("audit").query(
        KeyConditionExpression="document_id = :d",
        ExpressionAttributeValues={":d": "d1"})["Items"]
    assert [(i["from_status"], i["to_status"]) for i in items] == [
        (None, "PENDING_UPLOAD"), ("PENDING_UPLOAD", "RECEIVED")]


def test_redelivered_record_does_not_duplicate_audit(aws):
    base = {"sk": "META", "document_id": "d1", "status": "RECEIVED", "version": 2}
    rec = _record("101", base, base)
    stream_processor.handler({"Records": [rec]}, None)
    stream_processor.handler({"Records": [rec]}, None)
    count = aws["ddb"].Table("audit").scan()["Count"]
    assert count == 1


def _audit_entries(aws):
    return aws["ddb"].Table("audit").query(
        KeyConditionExpression="document_id = :d",
        ExpressionAttributeValues={":d": "d1"})["Items"]


def test_review_decision_records_reviewer_and_corrections(aws):
    base = {"sk": "META", "document_id": "d1"}
    corrections = {"TOTAL": {"previous": "1,200", "corrected": "1,250.00"}}
    stream_processor.handler({"Records": [_record(
        "5",
        {**base, "status": "PENDING_REVIEW", "version": 6, "claimed_by": "asha"},
        {**base, "status": "VALIDATED", "version": 7, "reviewed_by": "asha",
         "review_corrections": corrections},
    )]}, None)
    entry = _audit_entries(aws)[0]
    assert entry["actor"] == "asha"
    assert entry["action"] == "review_decision"
    assert entry["corrections"] == corrections


def test_claims_are_audited(aws):
    base = {"sk": "META", "document_id": "d1", "status": "PENDING_REVIEW"}
    stream_processor.handler({"Records": [
        _record("6", {**base, "version": 6}, {**base, "version": 7, "claimed_by": "ravi"}),
        _record("7", {**base, "version": 7, "claimed_by": "ravi"}, {**base, "version": 8}),
    ]}, None)
    assert [(e["action"], e["actor"]) for e in _audit_entries(aws)] == [
        ("claim", "ravi"), ("release", "ravi")]


def test_system_steps_have_no_actor(aws):
    base = {"sk": "META", "document_id": "d1"}
    stream_processor.handler({"Records": [_record(
        "8", {**base, "status": "SCANNED", "version": 3},
        {**base, "status": "EXTRACTED", "version": 4})]}, None)
    assert "actor" not in _audit_entries(aws)[0]


def test_bad_record_is_reported_individually(aws):
    good = _record("1", None, {"sk": "META", "document_id": "d1", "status": "X",
                                "version": 1}, "INSERT")
    bad = {"eventName": "MODIFY", "dynamodb": {"SequenceNumber": "2"}}  # malformed
    bad["dynamodb"]["NewImage"] = {"sk": {"S": "META"}}  # no document_id
    resp = stream_processor.handler({"Records": [good, bad]}, None)
    assert resp == {"batchItemFailures": [{"itemIdentifier": "2"}]}
