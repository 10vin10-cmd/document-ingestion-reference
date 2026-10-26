export type Queue = "retail" | "corporate" | "credit-notes";

export const QUEUES: { id: Queue; label: string }[] = [
  { id: "retail", label: "Retail" },
  { id: "corporate", label: "Corporate" },
  { id: "credit-notes", label: "Credit notes" },
];

export interface ExtractedField {
  value: string;
  confidence: number;
}

/** A row in the review queue (what the list endpoint projects). */
export interface ReviewSummary {
  document_id: string;
  business_queue: Queue;
  created_at: string;
  review_issues?: string[];
  possible_duplicate_of?: string[];
  claimed_by?: string;
  claim_expires_at?: string;
}

/** A document opened for review. */
export interface ReviewDocument extends ReviewSummary {
  status: string;
  version: number;
  fields: Record<string, ExtractedField>;
  content_type?: string;
  preview_url: string;
  template: { required_fields: string[]; min_confidence: number };
}

export type Decision =
  | { approved: true; corrected_fields: Record<string, string> }
  | { approved: false; reason: string };

export interface ReviewApi {
  listReviews(queue?: Queue): Promise<ReviewSummary[]>;
  /** Read-only view, for documents another reviewer holds. */
  getReview(id: string): Promise<ReviewDocument>;
  /** Take (or renew) the review lease and return the document. */
  claim(id: string): Promise<ReviewDocument>;
  release(id: string): Promise<void>;
  submit(id: string, decision: Decision): Promise<void>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Who holds the document, when the API refused because of a claim. */
  get claimedBy(): string | undefined {
    const holder = this.body.claimed_by;
    return typeof holder === "string" ? holder : undefined;
  }
}
