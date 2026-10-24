import pytest

from common.templates import get_template
from common.validation import assess


def _fields(**overrides):
    base = {
        "INVOICE_RECEIPT_ID": {"value": "INV-1", "confidence": 99.0},
        "INVOICE_RECEIPT_DATE": {"value": "2026-10-01", "confidence": 99.0},
        "VENDOR_NAME": {"value": "Acme", "confidence": 99.0},
        "TOTAL": {"value": "1,234.50", "confidence": 99.0},
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_complete_confident_document_passes():
    result = assess(_fields(), get_template("retail"))
    assert not result.needs_review
    assert result.issues == []


def test_missing_required_field_goes_to_review():
    result = assess(_fields(VENDOR_NAME=None), get_template("retail"))
    assert result.needs_review
    assert "missing:VENDOR_NAME" in result.issues


def test_low_confidence_goes_to_review():
    result = assess(_fields(TOTAL={"value": "10.00", "confidence": 80.0}),
                    get_template("retail"))
    assert "low_confidence:TOTAL" in result.issues


def test_unparseable_total_goes_to_review():
    result = assess(_fields(TOTAL={"value": "see attached", "confidence": 99.0}),
                    get_template("retail"))
    assert "unparseable:TOTAL" in result.issues


def test_local_date_format_is_flagged():
    result = assess(_fields(INVOICE_RECEIPT_DATE={"value": "01/10/2026", "confidence": 99.0}),
                    get_template("retail"))
    assert "unrecognised_date_format:INVOICE_RECEIPT_DATE" in result.issues


def test_queue_specific_fields_are_enforced():
    # Corporate invoices also need a tax registration number.
    result = assess(_fields(), get_template("corporate"))
    assert "missing:TAX_PAYER_ID" in result.issues


def test_unknown_queue_is_rejected():
    with pytest.raises(ValueError):
        get_template("nope")
