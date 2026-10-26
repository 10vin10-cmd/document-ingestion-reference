import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useApi } from "../api/context";
import { useSession } from "../auth/session";
import { age, heldByOther, isOverdue, issueText, parseIssue, shortId } from "../fields";
import { QUEUES, type Queue } from "../types";

const REFRESH_MS = 30_000;

export function ReviewQueue({ onOpen }: { onOpen: (id: string) => void }) {
  const api = useApi();
  const { username } = useSession();
  const [queue, setQueue] = useState<Queue | "">("");

  const { data, error, isPending, isFetching, refetch } = useQuery({
    queryKey: ["reviews", queue],
    queryFn: () => api.listReviews(queue || undefined),
    refetchInterval: REFRESH_MS,
  });

  const now = Date.now();
  const overdue = data?.filter((d) => isOverdue(d.created_at, now)).length ?? 0;

  return (
    <section className="queue" aria-labelledby="queue-title">
      <div className="queue-head">
        <div>
          <h1 id="queue-title">Review queue</h1>
          <p className="muted">
            {data
              ? `${data.length} waiting${overdue ? ` · ${overdue} over 24 hours` : ""} · oldest first`
              : "Documents the pipeline could not verify on its own"}
          </p>
        </div>
        <div className="queue-tools">
          <label className="select">
            <span>Queue</span>
            <select value={queue} onChange={(e) => setQueue(e.target.value as Queue | "")}>
              <option value="">All queues</option>
              {QUEUES.map((q) => (
                <option key={q.id} value={q.id}>{q.label}</option>
              ))}
            </select>
          </label>
          <button className="btn" onClick={() => void refetch()} disabled={isFetching}>
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && <p className="alert alert-error" role="alert">Could not load the queue: {error.message}</p>}
      {isPending && <p className="muted" role="status">Loading…</p>}
      {data && data.length === 0 && (
        <div className="empty">
          <p className="empty-title">Nothing waiting for review</p>
          <p className="muted">New documents appear here automatically.</p>
        </div>
      )}

      {data && data.length > 0 && (
        <table className="queue-table">
          <thead>
            <tr>
              <th scope="col">Document</th>
              <th scope="col">Queue</th>
              <th scope="col">Waiting</th>
              <th scope="col">Needs attention</th>
              <th scope="col"><span className="visually-hidden">Action</span></th>
            </tr>
          </thead>
          <tbody>
            {data.map((doc) => {
              const held = heldByOther(doc.claimed_by, doc.claim_expires_at, username, now);
              const late = isOverdue(doc.created_at, now);
              return (
                <tr key={doc.document_id} className={held ? "is-held" : undefined}>
                  <td data-label="Document">
                    <code>{shortId(doc.document_id)}</code>
                    {doc.possible_duplicate_of?.length ? <span className="tag tag-warn">Possible duplicate</span> : null}
                  </td>
                  <td data-label="Queue">{QUEUES.find((q) => q.id === doc.business_queue)?.label ?? doc.business_queue}</td>
                  <td data-label="Waiting">
                    <span className={late ? "overdue" : undefined} title={new Date(doc.created_at).toLocaleString()}>
                      {age(doc.created_at, now)}
                    </span>
                  </td>
                  <td data-label="Needs attention">
                    <ul className="chips">
                      {(doc.review_issues ?? []).map((raw) => (
                        <li key={raw} className="chip">{issueText(parseIssue(raw))}</li>
                      ))}
                    </ul>
                  </td>
                  <td className="row-action">
                    {held && <span className="held-by">In review by {doc.claimed_by}</span>}
                    <button
                      className={held ? "btn btn-small" : "btn btn-small btn-primary"}
                      onClick={() => onOpen(doc.document_id)}
                      aria-label={`${held ? "View" : "Review"} document ${shortId(doc.document_id)}`}
                    >
                      {held ? "View" : "Review"}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
