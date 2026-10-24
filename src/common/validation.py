"""Decide whether extracted fields can be accepted without a human.

Pure functions, no AWS calls, so the routing rule is easy to test and reason
about. Anything uncertain goes to review: a false "valid" is far more
expensive in accounts payable than a few minutes of reviewer time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from common.templates import Template


@dataclass
class Assessment:
    needs_review: bool
    issues: list[str] = field(default_factory=list)


def assess(fields: dict[str, dict], template: Template) -> Assessment:
    """``fields`` maps field type -> {"value": str, "confidence": float}."""
    issues: list[str] = []

    for name in template.required_fields:
        f = fields.get(name)
        if not f or not str(f.get("value", "")).strip():
            issues.append(f"missing:{name}")
        elif float(f.get("confidence", 0)) < template.min_confidence:
            issues.append(f"low_confidence:{name}")

    total = fields.get("TOTAL", {}).get("value")
    if total and _parse_amount(total) is None:
        issues.append("unparseable:TOTAL")

    invoice_date = fields.get("INVOICE_RECEIPT_DATE", {}).get("value")
    if invoice_date and _parse_iso_date(invoice_date) is None:
        # Not fatal: many invoices print local date formats. Flag for review.
        issues.append("unrecognised_date_format:INVOICE_RECEIPT_DATE")

    return Assessment(needs_review=bool(issues), issues=issues)


def _parse_amount(raw: str) -> Decimal | None:
    cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    try:
        return Decimal(cleaned) if cleaned else None
    except InvalidOperation:
        return None


def _parse_iso_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None
