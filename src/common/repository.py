"""DynamoDB access for document records.

Concurrency model: single-item optimistic concurrency. Every record carries a
``version`` attribute; a write succeeds only if the version it read is still
current *and* the record is in a state the transition is allowed from. Two
competing writers (for example a workflow step and a late reviewer callback)
cannot both win: the loser gets ``ConcurrencyError`` and re-reads.

This deliberately does not use DynamoDB's multi-item TransactWriteItems API:
contention here is on one item, so a conditional write is cheaper, simpler,
and sufficient.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common.states import ALLOWED_FROM, Status

HASH_INDEX = "by-content-hash"


class DocumentNotFound(Exception):
    pass


class DuplicateDocument(Exception):
    """A record with this document_id already exists (idempotent create)."""


class ConcurrencyError(Exception):
    """The record changed between our read and our write."""


class InvalidTransition(Exception):
    """The record is not in a state this transition may start from."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _pk(document_id: str) -> str:
    return f"DOC#{document_id}"


class DocumentRepository:
    def __init__(self, table: Any, clock: Callable[[], str] = _utc_now):
        self._table = table
        self._clock = clock

    # -- reads ---------------------------------------------------------------

    def get(self, document_id: str) -> dict:
        item = self._table.get_item(
            Key={"pk": _pk(document_id), "sk": "META"}, ConsistentRead=True
        ).get("Item")
        if item is None:
            raise DocumentNotFound(document_id)
        return item

    def find_by_content_hash(self, content_sha256: str) -> list[dict]:
        resp = self._table.query(
            IndexName=HASH_INDEX,
            KeyConditionExpression=Key("content_sha256").eq(content_sha256),
        )
        return resp.get("Items", [])

    # -- writes --------------------------------------------------------------

    def create(
        self,
        document_id: str,
        business_queue: str,
        s3_key: str,
        content_sha256: str | None = None,
        source: str = "api",
    ) -> dict:
        now = self._clock()
        item: dict[str, Any] = {
            "pk": _pk(document_id),
            "sk": "META",
            "document_id": document_id,
            "business_queue": business_queue,
            "s3_key": s3_key,
            "source": source,
            "status": Status.PENDING_UPLOAD.value,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        if content_sha256:
            item["content_sha256"] = content_sha256
            # Flag, never drop: possible duplicates go to a human. An automated
            # discard rule can silently lose a legitimate document.
            matches = [
                m["document_id"]
                for m in self.find_by_content_hash(content_sha256)
                if m["document_id"] != document_id
            ]
            if matches:
                item["possible_duplicate_of"] = sorted(matches)
        try:
            self._table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(pk)"
            )
        except ClientError as err:
            if _is_conditional_failure(err):
                raise DuplicateDocument(document_id) from err
            raise
        return item

    def transition(
        self,
        document_id: str,
        to_status: Status,
        expected_version: int,
        set_attrs: dict[str, Any] | None = None,
        remove_attrs: tuple[str, ...] = (),
    ) -> dict:
        allowed_from = sorted(s.value for s in ALLOWED_FROM[to_status])
        names = {"#status": "status", "#version": "version"}
        values: dict[str, Any] = {
            ":to": to_status.value,
            ":expected": expected_version,
            ":one": 1,
            ":now": self._clock(),
        }
        sets = ["#status = :to", "#version = #version + :one", "updated_at = :now"]
        for i, (attr, value) in enumerate((set_attrs or {}).items()):
            names[f"#a{i}"] = attr
            values[f":a{i}"] = value
            sets.append(f"#a{i} = :a{i}")

        from_placeholders = []
        for i, status in enumerate(allowed_from):
            values[f":from{i}"] = status
            from_placeholders.append(f":from{i}")

        update = "SET " + ", ".join(sets)
        if remove_attrs:
            for i, attr in enumerate(remove_attrs):
                names[f"#r{i}"] = attr
            update += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(remove_attrs)))

        try:
            resp = self._table.update_item(
                Key={"pk": _pk(document_id), "sk": "META"},
                UpdateExpression=update,
                ConditionExpression=(
                    "#version = :expected AND #status IN ("
                    + ", ".join(from_placeholders)
                    + ")"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as err:
            if not _is_conditional_failure(err):
                raise
            self._raise_specific_failure(document_id, to_status, expected_version)
        return resp["Attributes"]

    def _raise_specific_failure(
        self, document_id: str, to_status: Status, expected_version: int
    ) -> None:
        """Re-read after a failed condition so callers can react correctly:
        a stale version means retry with fresh state; a bad state does not."""
        current = self.get(document_id)
        if int(current["version"]) != expected_version:
            raise ConcurrencyError(
                f"{document_id}: expected version {expected_version}, "
                f"found {current['version']}"
            )
        raise InvalidTransition(
            f"{document_id}: cannot move {current['status']} -> {to_status.value}"
        )


def _is_conditional_failure(err: ClientError) -> bool:
    return err.response["Error"]["Code"] == "ConditionalCheckFailedException"
