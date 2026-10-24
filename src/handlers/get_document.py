"""GET /documents/{id} — current state of a document."""

from common import aws
from common.http import error, public_view, response
from common.repository import DocumentNotFound


def handler(event, _context):
    document_id = (event.get("pathParameters") or {}).get("id", "")
    try:
        item = aws.documents().get(document_id)
    except DocumentNotFound:
        return error(404, "document not found")
    return response(200, public_view(item))
