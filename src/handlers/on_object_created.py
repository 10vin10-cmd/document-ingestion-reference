"""S3 ObjectCreated -> mark the document received and start its workflow.

S3 delivers events at least once, so this handler must be safe to run twice
for the same object. Two guards make it so:

* the RECEIVED transition only succeeds from PENDING_UPLOAD, and
* the Step Functions execution is named after the document id, and Standard
  workflows reject a second execution with the same name.
"""

import json
import logging
import os
from urllib.parse import unquote_plus

from common import aws
from common.repository import DocumentNotFound, InvalidTransition
from common.states import Status

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, _context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        _process(bucket, key)


def _process(bucket: str, key: str) -> None:
    parts = key.split("/")
    if len(parts) < 3 or parts[0] != "incoming":
        log.warning("ignoring unexpected key %s", key)
        return
    document_id = parts[1]
    repo = aws.documents()

    try:
        item = repo.get(document_id)
    except DocumentNotFound:
        # Uploads must be registered first; an unregistered object is not ours.
        log.warning("no record for %s, ignoring", key)
        return

    if item["status"] == Status.PENDING_UPLOAD.value:
        try:
            repo.transition(document_id, Status.RECEIVED, int(item["version"]))
        except InvalidTransition:
            log.info("%s already advanced by a concurrent delivery", document_id)

    sfn = aws.client("stepfunctions")
    try:
        sfn.start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=document_id,
            input=json.dumps({"document_id": document_id, "bucket": bucket, "key": key}),
        )
    except sfn.exceptions.ExecutionAlreadyExists:
        log.info("workflow for %s already running (duplicate event)", document_id)
