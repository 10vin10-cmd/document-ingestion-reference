import { ApiError, type Decision, type Queue, type ReviewApi, type ReviewDocument, type ReviewSummary } from "../types";

/**
 * Reviewer API over HTTP. In production the API is served from the same
 * CloudFront origin as the app, so `baseUrl` is empty and no CORS is involved.
 *
 * The Cognito authorizer on a REST API expects the raw ID token in the
 * Authorization header (no "Bearer" prefix).
 */
export function createHttpApi(baseUrl: string, getToken: () => string | undefined): ReviewApi {
  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const token = getToken();
    if (!token) throw new ApiError(401, "Your session has expired. Sign in again.");

    const res = await fetch(`${baseUrl}${path}`, {
      method,
      headers: {
        Authorization: token,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (res.status === 204) return undefined as T;
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok) {
      const message = typeof data.error === "string" ? data.error : `Request failed (${res.status})`;
      throw new ApiError(res.status, message, data);
    }
    return data as T;
  }

  const doc = (id: string) => `/reviews/${encodeURIComponent(id)}`;

  return {
    async listReviews(queue?: Queue) {
      const query = queue ? `?queue=${encodeURIComponent(queue)}` : "";
      return (await call<{ items: ReviewSummary[] }>("GET", `/reviews${query}`)).items;
    },
    getReview: (id) => call<ReviewDocument>("GET", doc(id)),
    claim: (id) => call<ReviewDocument>("POST", `${doc(id)}/claim`),
    release: (id) => call<void>("POST", `${doc(id)}/release`),
    submit: (id, decision: Decision) => call<void>("POST", doc(id), decision),
  };
}
