import { createDemoApi } from "./demo";

describe("demo API", () => {
  it("follows the same claim rules as the service", async () => {
    const api = createDemoApi({ latencyMs: 0 });
    const docs = await api.listReviews();
    const held = docs.find((d) => d.claimed_by === "priya.s")!;

    await expect(api.claim(held.document_id)).rejects.toMatchObject({ status: 409, claimedBy: "priya.s" });
    await expect(api.getReview(held.document_id)).resolves.toMatchObject({ claimed_by: "priya.s" });
  });

  it("removes a document from the queue once decided", async () => {
    const api = createDemoApi({ latencyMs: 0 });
    const [first] = await api.listReviews();
    await api.claim(first!.document_id);
    await api.submit(first!.document_id, { approved: true, corrected_fields: {} });

    expect((await api.listReviews()).map((d) => d.document_id)).not.toContain(first!.document_id);
  });

  it("rejects approval with an empty required field", async () => {
    const api = createDemoApi({ latencyMs: 0 });
    const [first] = await api.listReviews();
    await expect(
      api.submit(first!.document_id, { approved: true, corrected_fields: { VENDOR_NAME: " " } }),
    ).rejects.toMatchObject({ status: 400 });
  });

  it("lists oldest first", async () => {
    const docs = await createDemoApi({ latencyMs: 0 }).listReviews();
    const times = docs.map((d) => d.created_at);
    expect(times).toEqual([...times].sort());
  });
});
