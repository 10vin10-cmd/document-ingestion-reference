"""Document lifecycle states and the transitions allowed between them.

The workflow is the only writer of most transitions, but two actors can race
on the same record (the orchestration steps and a human reviewer), so every
transition is checked against this table *and* guarded by a version check in
the repository layer.
"""

from enum import StrEnum


class Status(StrEnum):
    PENDING_UPLOAD = "PENDING_UPLOAD"  # record created, waiting for the file
    RECEIVED = "RECEIVED"  # file landed in S3, workflow started
    SCANNED = "SCANNED"  # passed malware / file-type screening
    QUARANTINED = "QUARANTINED"  # failed screening (terminal)
    EXTRACTED = "EXTRACTED"  # OCR / field extraction done
    PENDING_REVIEW = "PENDING_REVIEW"  # parked for a human, workflow paused
    VALIDATED = "VALIDATED"  # fields accepted (automatically or by a reviewer)
    REJECTED = "REJECTED"  # reviewer rejected the document (terminal)
    COMPLETED = "COMPLETED"  # published downstream (terminal)
    FAILED = "FAILED"  # unrecoverable processing error (terminal)


TERMINAL = frozenset(
    {Status.QUARANTINED, Status.REJECTED, Status.COMPLETED, Status.FAILED}
)

# to_status -> the statuses it may be entered from
ALLOWED_FROM: dict[Status, frozenset[Status]] = {
    Status.RECEIVED: frozenset({Status.PENDING_UPLOAD}),
    Status.SCANNED: frozenset({Status.RECEIVED}),
    Status.QUARANTINED: frozenset({Status.RECEIVED}),
    Status.EXTRACTED: frozenset({Status.SCANNED}),
    Status.PENDING_REVIEW: frozenset({Status.EXTRACTED}),
    Status.VALIDATED: frozenset({Status.EXTRACTED, Status.PENDING_REVIEW}),
    Status.REJECTED: frozenset({Status.PENDING_REVIEW}),
    Status.COMPLETED: frozenset({Status.VALIDATED}),
    Status.FAILED: frozenset(set(Status) - TERMINAL),
}


def can_transition(current: Status, target: Status) -> bool:
    return current in ALLOWED_FROM.get(target, frozenset())
