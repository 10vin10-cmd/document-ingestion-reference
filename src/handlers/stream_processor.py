"""DynamoDB Streams consumer: audit trail and (optionally) search index.

Every change to a document record arrives here in order per document. Writing
the audit trail from the stream, rather than from each handler, means no code
path can change a document without leaving an audit entry.

Audit entries go to a separate table so this consumer never triggers itself.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

log = logging.getLogger()
log.setLevel(logging.INFO)
_deserializer = TypeDeserializer()
_audit_table = None

SEARCHABLE = ("document_id", "business_queue", "status", "created_at",
              "updated_at", "possible_duplicate_of", "reviewed_by")


def handler(event, _context):
    failures = []
    for record in event.get("Records", []):
        try:
            _process(record)
        except Exception:  # report per record so only failures are retried
            log.exception("failed to process %s", record.get("eventID"))
            failures.append({"itemIdentifier": record["dynamodb"]["SequenceNumber"]})
    return {"batchItemFailures": failures}


def _process(record: dict) -> None:
    ddb = record["dynamodb"]
    new = _plain(ddb.get("NewImage"))
    old = _plain(ddb.get("OldImage"))
    current = new or old
    if not current or current.get("sk") != "META":
        return

    entry = {
        "document_id": current["document_id"],
        # Sequence numbers are unique and ordered within a shard, which makes
        # retried writes idempotent and keeps entries in order.
        "seq": f"{ddb['ApproximateCreationDateTime']:.0f}#{ddb['SequenceNumber']}",
        "event": record["eventName"],
        "from_status": (old or {}).get("status"),
        "to_status": (new or {}).get("status"),
        "version": (new or old).get("version"),
        "changed_at": current.get("updated_at"),
    }
    actor = _actor(old or {}, new or {})
    if actor:
        entry.update(actor)
    _audit().put_item(Item=entry)

    endpoint = os.environ.get("SEARCH_ENDPOINT")
    if endpoint and new:
        _index(endpoint, {k: new[k] for k in SEARCHABLE if k in new})


def _actor(old: dict, new: dict) -> dict | None:
    """Name the person behind a human action. System steps have no actor."""
    if old.get("status") == "PENDING_REVIEW" and new.get("status") in ("VALIDATED", "REJECTED"):
        out = {"actor": new.get("reviewed_by"), "action": "review_decision"}
        if new.get("review_corrections"):
            out["corrections"] = new["review_corrections"]
        if new.get("review_reason"):
            out["reason"] = new["review_reason"]
        return out
    if old.get("status") == new.get("status") and old.get("claimed_by") != new.get("claimed_by"):
        if new.get("claimed_by"):
            return {"actor": new["claimed_by"], "action": "claim"}
        return {"actor": old.get("claimed_by"), "action": "release"}
    return None


def _audit():
    global _audit_table
    if _audit_table is None:
        _audit_table = boto3.resource("dynamodb").Table(os.environ["AUDIT_TABLE_NAME"])
    return _audit_table


def _plain(image: dict | None) -> dict | None:
    if not image:
        return None
    return {k: _deserializer.deserialize(v) for k, v in image.items()}


def _index(endpoint: str, doc: dict) -> None:
    """PUT the document into an OpenSearch Serverless collection, signed with
    the function's role. Uses only the standard library and botocore."""
    url = f"{endpoint.rstrip('/')}/documents/_doc/{doc['document_id']}"
    body = json.dumps(doc, default=str).encode()
    req = AWSRequest(method="PUT", url=url, data=body,
                     headers={"Content-Type": "application/json"})
    session = boto3.Session()
    SigV4Auth(session.get_credentials(), "aoss", session.region_name).add_auth(req)
    with urllib.request.urlopen(
        urllib.request.Request(url, data=body, method="PUT", headers=dict(req.headers)),
        timeout=5,
    ) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"index request failed: {resp.status}")
