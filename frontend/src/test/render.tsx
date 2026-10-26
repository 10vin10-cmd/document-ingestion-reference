import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { ApiProvider } from "../api/context";
import { SessionProvider, type Session } from "../auth/session";
import type { ReviewApi, ReviewDocument, ReviewSummary } from "../types";

export const ME = "asha";

export function renderWithApp(ui: ReactElement, api: Partial<ReviewApi>) {
  const session: Session = { username: ME, displayName: "asha@example.com", signOut: vi.fn() };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const fullApi: ReviewApi = {
    listReviews: vi.fn().mockResolvedValue([]),
    getReview: vi.fn(),
    claim: vi.fn(),
    release: vi.fn().mockResolvedValue(undefined),
    submit: vi.fn().mockResolvedValue(undefined),
    ...api,
  };
  return {
    api: fullApi,
    ...render(
      <QueryClientProvider client={client}>
        <SessionProvider value={session}>
          <ApiProvider value={fullApi}>{ui}</ApiProvider>
        </SessionProvider>
      </QueryClientProvider>,
    ),
  };
}

const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();
const minutesFromNow = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

export function summary(overrides: Partial<ReviewSummary> = {}): ReviewSummary {
  return {
    document_id: "a1b2c3d4e5f6",
    business_queue: "corporate",
    created_at: minutesAgo(90),
    review_issues: ["low_confidence:TAX_PAYER_ID"],
    ...overrides,
  };
}

export const heldBy = (who: string) => ({ claimed_by: who, claim_expires_at: minutesFromNow(10) });

export function reviewDoc(overrides: Partial<ReviewDocument> = {}): ReviewDocument {
  return {
    ...summary(),
    status: "PENDING_REVIEW",
    version: 7,
    claimed_by: ME,
    claim_expires_at: minutesFromNow(15),
    content_type: "application/pdf",
    preview_url: "https://bucket.s3.ap-south-1.amazonaws.com/incoming/a1/invoice.pdf?X-Amz-Signature=x",
    template: {
      required_fields: ["INVOICE_RECEIPT_ID", "INVOICE_RECEIPT_DATE", "VENDOR_NAME", "TOTAL", "TAX_PAYER_ID"],
      min_confidence: 92,
    },
    fields: {
      INVOICE_RECEIPT_ID: { value: "INV-1001", confidence: 99 },
      INVOICE_RECEIPT_DATE: { value: "2026-10-01", confidence: 98 },
      VENDOR_NAME: { value: "Deccan Office Supplies", confidence: 97 },
      TOTAL: { value: "12,450.00", confidence: 96 },
      TAX_PAYER_ID: { value: "36AAAAA0000A1Z5", confidence: 88 },
    },
    ...overrides,
  };
}
