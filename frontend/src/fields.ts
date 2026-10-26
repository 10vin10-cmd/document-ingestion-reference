/** Display helpers for extracted fields, issues and timing. */

const LABELS: Record<string, string> = {
  INVOICE_RECEIPT_ID: "Invoice number",
  INVOICE_RECEIPT_DATE: "Invoice date",
  VENDOR_NAME: "Vendor",
  TOTAL: "Total",
  TAX_PAYER_ID: "Vendor tax ID (GSTIN)",
  SUBTOTAL: "Subtotal",
  TAX: "Tax",
  DUE_DATE: "Due date",
  PO_NUMBER: "PO number",
};

export const fieldLabel = (name: string) =>
  LABELS[name] ?? name.toLowerCase().replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export interface Issue {
  kind: "missing" | "low_confidence" | "unparseable" | "unrecognised_date_format" | "other";
  field?: string;
}

export function parseIssue(raw: string): Issue {
  const [kind, field] = raw.split(":");
  const known = ["missing", "low_confidence", "unparseable", "unrecognised_date_format"];
  return { kind: (known.includes(kind ?? "") ? kind : "other") as Issue["kind"], field };
}

export function issueText(issue: Issue): string {
  const field = issue.field ? fieldLabel(issue.field) : "";
  switch (issue.kind) {
    case "missing":
      return `${field} missing`;
    case "low_confidence":
      return `${field} uncertain`;
    case "unparseable":
      return `${field} unreadable`;
    case "unrecognised_date_format":
      return `${field} format`;
    default:
      return field || "Needs a look";
  }
}

/** "3h 12m", "2d 4h", "8m" */
export function age(fromIso: string, now = Date.now()): string {
  const minutes = Math.max(0, Math.floor((now - Date.parse(fromIso)) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export const OVERDUE_HOURS = 24;

export const isOverdue = (createdIso: string, now = Date.now()) =>
  now - Date.parse(createdIso) > OVERDUE_HOURS * 3_600_000;

export const shortId = (id: string) => id.slice(0, 8);

/** A claim by someone other than `me` that has not yet expired. */
export function heldByOther(claimedBy: string | undefined, expiresIso: string | undefined, me: string, now = Date.now()) {
  return !!claimedBy && claimedBy !== me && !!expiresIso && Date.parse(expiresIso) > now;
}
