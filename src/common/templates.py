"""Business queues and their capture templates.

A template is the set of fields a business line requires on its documents and
the confidence below which extraction is not trusted without a human. In a
production system this lives in a configuration service so business teams can
onboard a new queue without a deploy; it is static here to keep the reference
self-contained.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    required_fields: tuple[str, ...]
    min_confidence: float  # 0-100, as returned by Textract


# Field names follow Textract AnalyzeExpense summary-field types.
TEMPLATES: dict[str, Template] = {
    "retail": Template(
        required_fields=(
            "INVOICE_RECEIPT_ID",
            "INVOICE_RECEIPT_DATE",
            "VENDOR_NAME",
            "TOTAL",
        ),
        min_confidence=90.0,
    ),
    "corporate": Template(
        required_fields=(
            "INVOICE_RECEIPT_ID",
            "INVOICE_RECEIPT_DATE",
            "VENDOR_NAME",
            "TOTAL",
            "TAX_PAYER_ID",  # e.g. GSTIN for Indian vendors
        ),
        min_confidence=92.0,
    ),
    "credit-notes": Template(
        required_fields=("INVOICE_RECEIPT_ID", "VENDOR_NAME", "TOTAL"),
        min_confidence=95.0,
    ),
}


def get_template(business_queue: str) -> Template:
    try:
        return TEMPLATES[business_queue]
    except KeyError:
        raise ValueError(f"unknown business queue: {business_queue}") from None
