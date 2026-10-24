import pytest

from common.repository import (
    ConcurrencyError,
    DuplicateDocument,
    InvalidTransition,
)
from common.states import TERMINAL, Status, can_transition

HASH = "a" * 64


def _create(repo, doc_id="d1", sha=None):
    return repo.create(doc_id, "retail", f"incoming/{doc_id}/x.pdf", content_sha256=sha)


def test_create_starts_pending_at_version_1(aws):
    item = _create(aws["repo"])
    assert item["status"] == "PENDING_UPLOAD"
    assert item["version"] == 1
    assert aws["repo"].get("d1")["document_id"] == "d1"


def test_create_is_idempotent_on_document_id(aws):
    _create(aws["repo"])
    with pytest.raises(DuplicateDocument):
        _create(aws["repo"])


def test_same_content_is_flagged_not_dropped(aws):
    _create(aws["repo"], "d1", HASH)
    second = _create(aws["repo"], "d2", HASH)
    assert second["possible_duplicate_of"] == ["d1"]
    assert second["status"] == "PENDING_UPLOAD"  # still processed


def test_transition_advances_status_and_version(aws):
    _create(aws["repo"])
    item = aws["repo"].transition("d1", Status.RECEIVED, expected_version=1)
    assert item["status"] == "RECEIVED"
    assert item["version"] == 2


def test_stale_version_raises_concurrency_error(aws):
    repo = aws["repo"]
    _create(repo)
    repo.transition("d1", Status.RECEIVED, expected_version=1)
    with pytest.raises(ConcurrencyError):
        repo.transition("d1", Status.SCANNED, expected_version=1)


def test_disallowed_transition_raises(aws):
    _create(aws["repo"])
    with pytest.raises(InvalidTransition):
        aws["repo"].transition("d1", Status.COMPLETED, expected_version=1)


def test_only_one_of_two_racing_writers_wins(aws):
    repo = aws["repo"]
    _create(repo)
    version = int(repo.get("d1")["version"])  # both writers read version 1
    repo.transition("d1", Status.RECEIVED, version)
    with pytest.raises(ConcurrencyError):
        repo.transition("d1", Status.RECEIVED, version)
    assert repo.get("d1")["version"] == 2


def test_set_and_remove_attributes(aws):
    repo = aws["repo"]
    _create(repo)
    repo.transition("d1", Status.RECEIVED, 1, set_attrs={"note": "x"})
    item = repo.transition("d1", Status.SCANNED, 2, remove_attrs=("note",))
    assert "note" not in item


@pytest.mark.parametrize("terminal", sorted(TERMINAL, key=lambda s: s.value))
def test_terminal_states_cannot_fail(terminal):
    assert not can_transition(terminal, Status.FAILED)


def test_any_live_state_can_fail():
    for status in set(Status) - TERMINAL:
        assert can_transition(status, Status.FAILED)
