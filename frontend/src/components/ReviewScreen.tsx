import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";
import { useApi } from "../api/context";
import { useSession } from "../auth/session";
import { age, fieldLabel, parseIssue, shortId } from "../fields";
import { ApiError, QUEUES, type Decision, type ReviewApi, type ReviewDocument } from "../types";
import { DocumentPreview } from "./DocumentPreview";

interface Opened {
  doc: ReviewDocument;
  /** False when another reviewer holds the document: show it read-only. */
  mine: boolean;
}

/** Claim the document; if someone else holds it, fall back to a read-only view. */
async function open(api: ReviewApi, id: string): Promise<Opened> {
  try {
    return { doc: await api.claim(id), mine: true };
  } catch (err) {
    if (err instanceof ApiError && err.status === 409 && err.claimedBy) {
      return { doc: await api.getReview(id), mine: false };
    }
    throw err;
  }
}

export function ReviewScreen({ id, onClose }: { id: string; onClose: (notice?: string) => void }) {
  const api = useApi();
  const { data, error, isPending } = useQuery({
    queryKey: ["review", id],
    queryFn: () => open(api, id),
    // Opening claims the document; don't re-claim behind the reviewer's back.
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });

  const back = () => {
    if (data?.mine) void api.release(id).catch(() => undefined);
    onClose();
  };

  return (
    <section className="review" aria-labelledby="review-title">
      <button className="link-button" onClick={back}>← Back to queue</button>
      {isPending && <p className="muted" role="status">Opening document…</p>}
      {error && (
        <div className="alert alert-error" role="alert">
          {error instanceof ApiError && error.status === 409
            ? "This document is no longer waiting for review. Someone may have just completed it."
            : `Could not open the document: ${error.message}`}
        </div>
      )}
      {data && <ReviewForm key={data.doc.document_id} opened={data} onDone={onClose} />}
    </section>
  );
}

function ReviewForm({ opened, onDone }: { opened: Opened; onDone: (notice?: string) => void }) {
  const { doc, mine } = opened;
  const api = useApi();
  const { username } = useSession();
  const queryClient = useQueryClient();
  const reasonId = useId();

  const required = doc.template.required_fields;
  const names = [...required, ...Object.keys(doc.fields).filter((n) => !required.includes(n)).sort()];
  const original = Object.fromEntries(names.map((n) => [n, doc.fields[n]?.value ?? ""]));

  const [values, setValues] = useState<Record<string, string>>(original);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  const changed = Object.fromEntries(
    Object.entries(values).filter(([n, v]) => v.trim() !== (original[n] ?? "").trim()).map(([n, v]) => [n, v.trim()]),
  );
  const emptyRequired = required.filter((n) => !values[n]?.trim());

  const issuesByField = new Map<string, string[]>();
  for (const issue of (doc.review_issues ?? []).map(parseIssue)) {
    if (issue.field) issuesByField.set(issue.field, [...(issuesByField.get(issue.field) ?? []), issue.kind]);
  }

  const submit = useMutation({
    mutationFn: (decision: Decision) => api.submit(doc.document_id, decision),
    onSuccess: (_, decision) => {
      void queryClient.invalidateQueries({ queryKey: ["reviews"] });
      const label = values.INVOICE_RECEIPT_ID || shortId(doc.document_id);
      onDone(decision.approved ? `Approved ${label}` : `Rejected ${label}`);
    },
  });

  const conflict = submit.error instanceof ApiError && submit.error.status === 409;
  const queueLabel = QUEUES.find((q) => q.id === doc.business_queue)?.label ?? doc.business_queue;
  const holder = doc.claimed_by && doc.claimed_by !== username ? doc.claimed_by : undefined;

  return (
    <>
      <header className="review-head">
        <h1 id="review-title">
          {values.VENDOR_NAME || "Unknown vendor"}
          <span className="muted"> · {values.INVOICE_RECEIPT_ID || shortId(doc.document_id)}</span>
        </h1>
        <p className="muted">
          {queueLabel} queue · received {age(doc.created_at)} ago · document <code>{shortId(doc.document_id)}</code>
        </p>
      </header>

      {!mine && (
        <p className="alert alert-info" role="status">
          <strong>{holder ?? "Another reviewer"}</strong> is reviewing this document. You can look, but not change it.
        </p>
      )}
      {doc.possible_duplicate_of?.length ? (
        <p className="alert alert-warn">
          The same file was received before as document{doc.possible_duplicate_of.length > 1 ? "s" : ""}{" "}
          {doc.possible_duplicate_of.map((d) => <code key={d}>{shortId(d)}</code>)}. Check it isn&apos;t a resubmission
          before approving.
        </p>
      ) : null}

      <div className="review-body">
        <DocumentPreview url={doc.preview_url} contentType={doc.content_type} />

        <form
          className="fields"
          aria-label="Extracted fields"
          onSubmit={(e) => {
            e.preventDefault();
            submit.mutate({ approved: true, corrected_fields: changed });
          }}
        >
          <h2>Extracted fields</h2>
          <p className="muted small">
            Fields below {doc.template.min_confidence}% confidence are flagged. Correct anything that doesn&apos;t
            match the document.
          </p>

          {names.map((name) => {
            const field = doc.fields[name];
            const kinds = issuesByField.get(name) ?? [];
            const isRequired = required.includes(name);
            const edited = (values[name] ?? "").trim() !== (original[name] ?? "").trim();
            const state = edited ? "edited" : !field?.value ? "missing" : kinds.length ? "flagged" : "ok";
            const hintId = `${name}-hint`;
            return (
              <div key={name} className={`field field-${state}`}>
                <label htmlFor={name}>
                  {fieldLabel(name)}
                  {isRequired && <span className="required" aria-hidden="true"> *</span>}
                </label>
                <input
                  id={name}
                  value={values[name] ?? ""}
                  onChange={(e) => setValues((v) => ({ ...v, [name]: e.target.value }))}
                  disabled={!mine || submit.isPending}
                  required={isRequired}
                  aria-invalid={state === "missing" || state === "flagged" ? true : undefined}
                  aria-describedby={hintId}
                  spellCheck={false}
                  autoComplete="off"
                />
                <span id={hintId} className="field-hint">
                  {fieldHint(state, field?.confidence, kinds)}
                </span>
              </div>
            );
          })}

          {submit.error && (
            <p className="alert alert-error" role="alert">
              {conflict
                ? submit.error instanceof ApiError && submit.error.claimedBy
                  ? `${submit.error.claimedBy} has taken over this document.`
                  : "Someone else completed this document while you were working on it."
                : submit.error.message}
            </p>
          )}

          {mine && !rejecting && (
            <div className="actions">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={submit.isPending || emptyRequired.length > 0 || conflict}
              >
                {submit.isPending
                  ? "Saving…"
                  : Object.keys(changed).length
                    ? `Approve with ${Object.keys(changed).length} correction${Object.keys(changed).length > 1 ? "s" : ""}`
                    : "Approve"}
              </button>
              <button type="button" className="btn btn-danger-outline" onClick={() => setRejecting(true)} disabled={submit.isPending || conflict}>
                Reject…
              </button>
              {emptyRequired.length > 0 && (
                <p className="muted small">Fill in {emptyRequired.map(fieldLabel).join(", ")} to approve.</p>
              )}
            </div>
          )}

          {mine && rejecting && (
            <div className="reject">
              <label htmlFor={reasonId}>Why are you rejecting this document?</label>
              <textarea
                id={reasonId}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                rows={3}
                maxLength={1000}
                placeholder="For example: duplicate of INV-1001, or not addressed to us"
                autoFocus
              />
              <div className="actions">
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={!reason.trim() || submit.isPending || conflict}
                  onClick={() => submit.mutate({ approved: false, reason: reason.trim() })}
                >
                  {submit.isPending ? "Saving…" : "Confirm rejection"}
                </button>
                <button type="button" className="btn" onClick={() => setRejecting(false)} disabled={submit.isPending}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </form>
      </div>
    </>
  );
}

function fieldHint(state: string, confidence: number | undefined, kinds: string[]): string {
  if (state === "edited") return "Corrected by you";
  if (state === "missing") return "Not found on the document";
  const pct = confidence === undefined ? "" : `${Math.round(confidence)}% confidence`;
  if (kinds.includes("unparseable")) return `Couldn't read this as an amount · ${pct}`;
  if (kinds.includes("unrecognised_date_format")) return `Use YYYY-MM-DD · ${pct}`;
  if (kinds.includes("low_confidence")) return `Low confidence · ${pct}`;
  return pct;
}
