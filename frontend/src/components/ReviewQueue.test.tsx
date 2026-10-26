import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { heldBy, renderWithApp, summary } from "../test/render";
import { ReviewQueue } from "./ReviewQueue";

describe("ReviewQueue", () => {
  it("lists waiting documents with readable issues", async () => {
    renderWithApp(<ReviewQueue onOpen={vi.fn()} />, {
      listReviews: vi.fn().mockResolvedValue([
        summary({ review_issues: ["low_confidence:TAX_PAYER_ID", "missing:VENDOR_NAME"] }),
      ]),
    });

    const row = (await screen.findAllByRole("row"))[1]!;
    expect(within(row).getByText("Vendor tax ID (GSTIN) uncertain")).toBeInTheDocument();
    expect(within(row).getByText("Vendor missing")).toBeInTheDocument();
    expect(within(row).getByText("Corporate")).toBeInTheDocument();
    expect(screen.getByText(/1 waiting/)).toBeInTheDocument();
  });

  it("flags documents waiting more than a day", async () => {
    renderWithApp(<ReviewQueue onOpen={vi.fn()} />, {
      listReviews: vi.fn().mockResolvedValue([
        summary({ created_at: new Date(Date.now() - 30 * 3_600_000).toISOString() }),
      ]),
    });
    expect(await screen.findByText("1d 6h")).toHaveClass("overdue");
    expect(screen.getByText(/1 over 24 hours/)).toBeInTheDocument();
  });

  it("shows who holds a document and offers a read-only view", async () => {
    const onOpen = vi.fn();
    renderWithApp(<ReviewQueue onOpen={onOpen} />, {
      listReviews: vi.fn().mockResolvedValue([summary({ ...heldBy("ravi") })]),
    });

    expect(await screen.findByText("In review by ravi")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /view document a1b2c3d4/i }));
    expect(onOpen).toHaveBeenCalledWith("a1b2c3d4e5f6");
  });

  it("treats your own claim as available", async () => {
    renderWithApp(<ReviewQueue onOpen={vi.fn()} />, {
      listReviews: vi.fn().mockResolvedValue([summary({ ...heldBy("asha") })]),
    });
    expect(await screen.findByRole("button", { name: /review document/i })).toBeInTheDocument();
    expect(screen.queryByText(/in review by/i)).not.toBeInTheDocument();
  });

  it("filters by queue", async () => {
    const listReviews = vi.fn().mockResolvedValue([]);
    renderWithApp(<ReviewQueue onOpen={vi.fn()} />, { listReviews });
    await screen.findByText("Nothing waiting for review");

    await userEvent.selectOptions(screen.getByRole("combobox", { name: /queue/i }), "retail");
    expect(listReviews).toHaveBeenLastCalledWith("retail");
  });

  it("reports load failures", async () => {
    renderWithApp(<ReviewQueue onOpen={vi.fn()} />, {
      listReviews: vi.fn().mockRejectedValue(new Error("network down")),
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
  });
});
