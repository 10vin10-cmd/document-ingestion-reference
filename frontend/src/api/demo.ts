import { ApiError, type Decision, type ExtractedField, type Queue, type ReviewApi, type ReviewDocument } from "../types";

/**
 * In-browser stand-in for the reviewer API, used by `npm run dev:demo` and the
 * demo build. It follows the same rules as the real service: claims are
 * leases, a document held by someone else is read-only, rejection needs a
 * reason and approval needs every required field.
 */

export const DEMO_USER = "demo.reviewer";
const OTHER_REVIEWER = "priya.s";
const CLAIM_MINUTES = 15;

const TEMPLATES: Record<Queue, { required_fields: string[]; min_confidence: number }> = {
  retail: { required_fields: ["INVOICE_RECEIPT_ID", "INVOICE_RECEIPT_DATE", "VENDOR_NAME", "TOTAL"], min_confidence: 90 },
  corporate: {
    required_fields: ["INVOICE_RECEIPT_ID", "INVOICE_RECEIPT_DATE", "VENDOR_NAME", "TOTAL", "TAX_PAYER_ID"],
    min_confidence: 92,
  },
  "credit-notes": { required_fields: ["INVOICE_RECEIPT_ID", "VENDOR_NAME", "TOTAL"], min_confidence: 95 },
};

interface Seed {
  id: string;
  queue: Queue;
  minutesAgo: number;
  fields: Record<string, ExtractedField>;
  issues: string[];
  duplicateOf?: string[];
  heldByOther?: boolean;
}

const f = (value: string, confidence: number): ExtractedField => ({ value, confidence });

const SEEDS: Seed[] = [
  {
    id: "7f3c9a1e52d84b6f9c0e1a2b3c4d5e6f",
    queue: "corporate",
    minutesAgo: 26 * 60 + 14,
    fields: {
      INVOICE_RECEIPT_ID: f("INV-20431", 99.2),
      INVOICE_RECEIPT_DATE: f("2026-10-21", 97.8),
      VENDOR_NAME: f("Deccan Office Supplies Pvt Ltd", 98.4),
      TOTAL: f("48,260.00", 96.1),
      TAX_PAYER_ID: f("36AAACD1234F1Z5", 88.0),
    },
    issues: ["low_confidence:TAX_PAYER_ID"],
  },
  {
    id: "a91b2c3d4e5f60718293a4b5c6d7e8f9",
    queue: "retail",
    minutesAgo: 5 * 60 + 2,
    fields: {
      INVOICE_RECEIPT_ID: f("BPW/2026/1187", 95.5),
      INVOICE_RECEIPT_DATE: f("2026-10-24", 98.9),
      VENDOR_NAME: f("Banjara Print Works", 97.0),
      TOTAL: f("12,4S0.00", 71.3),
    },
    issues: ["low_confidence:TOTAL", "unparseable:TOTAL"],
  },
  {
    id: "c0ffee12345678909876543210abcdef",
    queue: "retail",
    minutesAgo: 3 * 60 + 40,
    fields: {
      INVOICE_RECEIPT_ID: f("KPC-55821", 98.7),
      INVOICE_RECEIPT_DATE: f("03/10/2026", 96.0),
      VENDOR_NAME: f("Kaveri Packaging Co.", 99.1),
      TOTAL: f("7,915.50", 98.2),
    },
    issues: ["unrecognised_date_format:INVOICE_RECEIPT_DATE"],
    duplicateOf: ["5d6e7f8091a2b3c4d5e6f708192a3b4c"],
  },
  {
    id: "e4d3c2b1a09f8e7d6c5b4a3928170615",
    queue: "credit-notes",
    minutesAgo: 2 * 60 + 5,
    fields: {
      INVOICE_RECEIPT_ID: f("CN-0912", 99.0),
      VENDOR_NAME: f("", 0),
      TOTAL: f("-3,200.00", 97.6),
    },
    issues: ["missing:VENDOR_NAME"],
    heldByOther: true,
  },
  {
    id: "1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e",
    queue: "corporate",
    minutesAgo: 41,
    fields: {
      INVOICE_RECEIPT_ID: f("GL-24-00917", 84.6),
      INVOICE_RECEIPT_DATE: f("2026-10-25", 98.1),
      VENDOR_NAME: f("Godavari Logistics LLP", 96.9),
      TOTAL: f("1,12,400.00", 95.4),
      TAX_PAYER_ID: f("36AAGFG5678K1Z2", 97.3),
    },
    issues: ["low_confidence:INVOICE_RECEIPT_ID"],
  },
];

const minutesFromNow = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

function toDocument(seed: Seed): ReviewDocument {
  return {
    document_id: seed.id,
    business_queue: seed.queue,
    created_at: minutesFromNow(-seed.minutesAgo),
    review_issues: seed.issues,
    possible_duplicate_of: seed.duplicateOf,
    claimed_by: seed.heldByOther ? OTHER_REVIEWER : undefined,
    claim_expires_at: seed.heldByOther ? minutesFromNow(9) : undefined,
    status: "PENDING_REVIEW",
    version: 6,
    fields: seed.fields,
    content_type: "image/svg+xml",
    preview_url: invoiceImage(seed),
    template: TEMPLATES[seed.queue],
  };
}

export function createDemoApi({ latencyMs = 350 } = {}): ReviewApi {
  const store = new Map(SEEDS.map((s) => [s.id, toDocument(s)]));
  const wait = () => new Promise((resolve) => setTimeout(resolve, latencyMs));

  const find = (id: string) => {
    const doc = store.get(id);
    if (!doc) throw new ApiError(404, "document not found");
    return doc;
  };
  const heldByOther = (doc: ReviewDocument) =>
    !!doc.claimed_by && doc.claimed_by !== DEMO_USER && Date.parse(doc.claim_expires_at ?? "") > Date.now();

  return {
    async listReviews(queue) {
      await wait();
      return [...store.values()]
        .filter((d) => !queue || d.business_queue === queue)
        .sort((a, b) => a.created_at.localeCompare(b.created_at))
        .map(({ document_id, business_queue, created_at, review_issues, possible_duplicate_of, claimed_by, claim_expires_at }) => ({
          document_id, business_queue, created_at, review_issues, possible_duplicate_of, claimed_by, claim_expires_at,
        }));
    },
    async getReview(id) {
      await wait();
      return structuredClone(find(id));
    },
    async claim(id) {
      await wait();
      const doc = find(id);
      if (heldByOther(doc)) throw new ApiError(409, "claimed by another reviewer", { claimed_by: doc.claimed_by });
      doc.claimed_by = DEMO_USER;
      doc.claim_expires_at = minutesFromNow(CLAIM_MINUTES);
      doc.version += 1;
      return structuredClone(doc);
    },
    async release(id) {
      await wait();
      const doc = store.get(id);
      if (doc?.claimed_by === DEMO_USER) {
        doc.claimed_by = undefined;
        doc.claim_expires_at = undefined;
      }
    },
    async submit(id, decision: Decision) {
      await wait();
      const doc = find(id);
      if (heldByOther(doc)) throw new ApiError(409, "claimed by another reviewer", { claimed_by: doc.claimed_by });
      if (!decision.approved && !decision.reason.trim()) throw new ApiError(400, "a reason is required to reject a document");
      if (decision.approved) {
        const merged = { ...Object.fromEntries(Object.entries(doc.fields).map(([k, v]) => [k, v.value])), ...decision.corrected_fields };
        const missing = doc.template.required_fields.filter((name) => !merged[name]?.trim());
        if (missing.length) throw new ApiError(400, `required fields are empty: ${missing.join(", ")}`);
      }
      store.delete(id);
    },
  };
}

/** A simple rendered invoice so the demo has something to look at. */
function invoiceImage(seed: Seed): string {
  const v = (name: string) => escapeXml(seed.fields[name]?.value ?? "");
  const credit = seed.queue === "credit-notes";
  const vendor = v("VENDOR_NAME") || "&#160;";
  const rows = [
    ["Professional services, October", "1", v("TOTAL")],
  ];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="620" height="820" viewBox="0 0 620 820">
  <rect width="620" height="820" fill="#fdfcf8"/>
  <rect x="0" y="0" width="620" height="8" fill="#2f4858"/>
  <text x="48" y="78" font-family="Georgia, serif" font-size="24" fill="#1d2a33">${vendor}</text>
  <text x="48" y="102" font-family="Arial, sans-serif" font-size="12" fill="#5a6670">Plot 14, Industrial Area Phase II, Hyderabad 500032</text>
  ${seed.fields.TAX_PAYER_ID ? `<text x="48" y="120" font-family="Arial, sans-serif" font-size="12" fill="#5a6670">GSTIN ${v("TAX_PAYER_ID")}</text>` : ""}
  <text x="572" y="78" text-anchor="end" font-family="Arial, sans-serif" font-size="22" font-weight="bold" fill="#2f4858">${credit ? "CREDIT NOTE" : "TAX INVOICE"}</text>
  <text x="572" y="106" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#1d2a33">No. ${v("INVOICE_RECEIPT_ID")}</text>
  ${seed.fields.INVOICE_RECEIPT_DATE ? `<text x="572" y="126" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#1d2a33">Date ${v("INVOICE_RECEIPT_DATE")}</text>` : ""}
  <text x="48" y="190" font-family="Arial, sans-serif" font-size="11" fill="#5a6670">BILL TO</text>
  <text x="48" y="210" font-family="Arial, sans-serif" font-size="14" fill="#1d2a33">Accounts Payable, Finance Shared Services</text>
  <rect x="48" y="260" width="524" height="30" fill="#e8edf0"/>
  <text x="60" y="280" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#2f4858">DESCRIPTION</text>
  <text x="430" y="280" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#2f4858">QTY</text>
  <text x="560" y="280" text-anchor="end" font-family="Arial, sans-serif" font-size="12" font-weight="bold" fill="#2f4858">AMOUNT</text>
  ${rows.map((r, i) => `<text x="60" y="${318 + i * 26}" font-family="Arial, sans-serif" font-size="13" fill="#1d2a33">${r[0]}</text>
  <text x="430" y="${318 + i * 26}" font-family="Arial, sans-serif" font-size="13" fill="#1d2a33">${r[1]}</text>
  <text x="560" y="${318 + i * 26}" text-anchor="end" font-family="Arial, sans-serif" font-size="13" fill="#1d2a33">${r[2]}</text>`).join("")}
  <line x1="48" y1="560" x2="572" y2="560" stroke="#c9d2d8"/>
  <text x="400" y="596" font-family="Arial, sans-serif" font-size="14" fill="#1d2a33">Total (INR)</text>
  <text x="560" y="596" text-anchor="end" font-family="Arial, sans-serif" font-size="18" font-weight="bold" fill="#1d2a33">${v("TOTAL")}</text>
  <text x="48" y="760" font-family="Arial, sans-serif" font-size="10" fill="#8a949b">Sample document generated for the demo. Not a real invoice.</text>
</svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function escapeXml(text: string): string {
  return text.replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" })[c]!);
}
