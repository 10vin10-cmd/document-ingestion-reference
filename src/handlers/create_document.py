"""POST /documents — register a document and hand back an upload URL.

Machine clients authenticate with SigV4 (IAM auth on API Gateway). The file
itself never passes through the API: clients PUT it straight to S3 with a
short-lived presigned URL, which keeps Lambda payload limits and cost out of
the upload path.

Idempotency: if the caller sends an ``Idempotency-Key`` header, the document
id is derived from it, so a retried request returns the same document instead
of creating a second one.
"""

import os
import re
import uuid

from common import aws
from common.http import error, json_body, public_view, response
from common.repository import DuplicateDocument
from common.templates import TEMPLATES

UPLOAD_URL_TTL_SECONDS = 900
_ID_NAMESPACE = uuid.UUID("6f1c2f5e-2b8a-4e0e-9a1f-4c3d2b1a0e9f")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def handler(event, _context):
    try:
        body = json_body(event)
    except ValueError as err:
        return error(400, str(err))

    queue = body.get("business_queue")
    if queue not in TEMPLATES:
        return error(400, f"business_queue must be one of {sorted(TEMPLATES)}")
    file_name = _SAFE_NAME.sub("_", str(body.get("file_name") or "document.pdf"))[:128]
    sha256 = body.get("content_sha256")
    if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
        return error(400, "content_sha256 must be 64 lowercase hex characters")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    idem_key = headers.get("idempotency-key")
    document_id = (
        uuid.uuid5(_ID_NAMESPACE, idem_key).hex if idem_key else uuid.uuid4().hex
    )
    s3_key = f"incoming/{document_id}/{file_name}"

    repo = aws.documents()
    status_code = 201
    try:
        item = repo.create(document_id, queue, s3_key, content_sha256=sha256)
    except DuplicateDocument:
        item = repo.get(document_id)
        status_code = 200

    body_out = public_view(item)
    if item["status"] == "PENDING_UPLOAD":
        body_out["upload_url"] = aws.client("s3").generate_presigned_url(
            "put_object",
            Params={"Bucket": os.environ["BUCKET_NAME"], "Key": item["s3_key"]},
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        )
    return response(status_code, body_out)
