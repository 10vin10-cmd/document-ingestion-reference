import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { heldBy, renderWithApp, reviewDoc } from "../test/render";
import { ApiError } from "../types";
import { ReviewScreen } from "./ReviewScreen";

const open = (api: Parameters<typeof renderWithApp>[1], onClose = vi.fn()) => ({
  onClose,
  ...renderWithApp(<ReviewScreen id="a1b2c3d4e5f6" onClose={onClose} />, api),
});

describe("ReviewScreen", () => {
  it("claims the document and flags low-confidence fields", async () => {
    const { api } = open({ claim: vi.fn().mockResolvedValue(reviewDoc()) });

    const taxId = await screen.findByLabelText(/vendor tax id/i);
    expect(api.claim).toHaveBeenCalledWith("a1b2c3d4e5f6");
    expect(taxId).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Low confidence · 88% confidence")).toBeInTheDocument();
    expect(screen.getByTitle("Original document")).toHaveAttribute("src", expect.stringContaining("invoice.pdf"));
  });

  it("sends only the fields the reviewer changed", async () => {
    const { api, onClose } = open({ claim: vi.fn().mockResolvedValue(reviewDoc()) });

    const taxId = await screen.findByLabelText(/vendor tax id/i);
    await userEvent.clear(taxId);
    await userEvent.type(taxId, "36ABCDE1234F1Z5");
    expect(screen.getByText("Corrected by you")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Approve with 1 correction" }));
    expect(api.submit).toHaveBeenCalledWith("a1b2c3d4e5f6", {
      approved: true,
      corrected_fields: { TAX_PAYER_ID: "36ABCDE1234F1Z5" },
    });
    expect(onClose).toHaveBeenCalledWith("Approved INV-1001");
  });

  it("won't approve while a required field is empty", async () => {
    open({ claim: vi.fn().mockResolvedValue(reviewDoc()) });

    await userEvent.clear(await screen.findByRole("textbox", { name: "Vendor" }));
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByText("Fill in Vendor to approve.")).toBeInTheDocument();
  });

  it("requires a reason to reject", async () => {
    const { api } = open({ claim: vi.fn().mockResolvedValue(reviewDoc()) });

    await userEvent.click(await screen.findByRole("button", { name: "Reject…" }));
    const confirm = screen.getByRole("button", { name: "Confirm rejection" });
    expect(confirm).toBeDisabled();

    await userEvent.type(screen.getByLabelText(/why are you rejecting/i), "Duplicate of INV-0999");
    await userEvent.click(confirm);
    expect(api.submit).toHaveBeenCalledWith("a1b2c3d4e5f6", { approved: false, reason: "Duplicate of INV-0999" });
  });

  it("opens read-only when another reviewer holds the document", async () => {
    const { api } = open({
      claim: vi.fn().mockRejectedValue(new ApiError(409, "claimed", { claimed_by: "ravi" })),
      getReview: vi.fn().mockResolvedValue(reviewDoc({ ...heldBy("ravi") })),
    });

    expect(await screen.findByText(/is reviewing this document/)).toHaveTextContent("ravi");
    expect(api.getReview).toHaveBeenCalledWith("a1b2c3d4e5f6");
    expect(screen.getByRole("textbox", { name: "Vendor" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /approve/i })).not.toBeInTheDocument();
  });

  it("explains when someone else finished the document first", async () => {
    open({
      claim: vi.fn().mockResolvedValue(reviewDoc()),
      submit: vi.fn().mockRejectedValue(new ApiError(409, "updated by someone else")),
    });

    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else completed this document");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("warns about possible duplicates", async () => {
    open({ claim: vi.fn().mockResolvedValue(reviewDoc({ possible_duplicate_of: ["ffeeddccbbaa99"] })) });
    expect(await screen.findByText(/same file was received before/)).toHaveTextContent("ffeeddcc");
  });

  it("releases the claim when going back", async () => {
    const { api, onClose } = open({ claim: vi.fn().mockResolvedValue(reviewDoc()) });
    await screen.findByLabelText(/vendor tax id/i);

    await userEvent.click(screen.getByRole("button", { name: /back to queue/i }));
    expect(api.release).toHaveBeenCalledWith("a1b2c3d4e5f6");
    expect(onClose).toHaveBeenCalled();
  });

  it("explains when the document has already left review", async () => {
    open({ claim: vi.fn().mockRejectedValue(new ApiError(409, "document is VALIDATED, not awaiting review")) });
    expect(await screen.findByRole("alert")).toHaveTextContent("no longer waiting for review");
  });
});
