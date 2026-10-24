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


def test_bad_record_is_reported_individually(aws):
    good = _record("1", None, {"sk": "META", "document_id": "d1", "status": "X",
                                "version": 1}, "INSERT")
    bad = {"eventName": "MODIFY", "dynamodb": {"SequenceNumber": "2"}}  # malformed
    bad["dynamodb"]["NewImage"] = {"sk": {"S": "META"}}  # no document_id
    resp = stream_processor.handler({"Records": [good, bad]}, None)
    assert resp == {"batchItemFailures": [{"itemIdentifier": "2"}]}
