import { ApiError } from "../types";
import { createHttpApi } from "./client";

function respond(status: number, body?: unknown) {
  return vi.fn().mockResolvedValue(
    new Response(body === undefined ? null : JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("createHttpApi", () => {
  it("sends the raw ID token, as the Cognito authorizer expects", async () => {
    const fetchMock = respond(200, { items: [] });
    vi.stubGlobal("fetch", fetchMock);

    await createHttpApi("", () => "id-token").listReviews("retail");

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/reviews?queue=retail");
    expect(init.headers.Authorization).toBe("id-token");
  });

  it("posts decisions as JSON", async () => {
    const fetchMock = respond(200, {});
    vi.stubGlobal("fetch", fetchMock);

    await createHttpApi("https://api.example.com", () => "t").submit("doc/1", { approved: false, reason: "dup" });

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("https://api.example.com/reviews/doc%2F1");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ approved: false, reason: "dup" });
  });

  it("turns error responses into ApiError with the holder of a claim", async () => {
    vi.stubGlobal("fetch", respond(409, { error: "claimed by another reviewer", claimed_by: "ravi" }));

    const err = await createHttpApi("", () => "t").claim("d1").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).claimedBy).toBe("ravi");
  });

  it("handles empty 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(createHttpApi("", () => "t").release("d1")).resolves.toBeUndefined();
  });

  it("refuses to call the API without a token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(createHttpApi("", () => undefined).listReviews()).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
