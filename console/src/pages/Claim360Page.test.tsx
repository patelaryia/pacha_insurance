import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Claim360, ConsoleApi } from "../api/types";
import { Claim360Page } from "./Claim360Page";
import { ReviewQueuePage } from "./ReviewQueuePage";

afterEach(cleanup);

const claim: Claim360 = {
  claim: { id: "claim-1", status: "RESERVED", substatus: null, assigned_to: "user:1", created_at: "2026-07-14T07:00:00Z", updated_at: "2026-07-14T08:00:00Z" },
  header: { insured: "Amina", registration: "KAA 111B", amount_cents: 123456n },
  fields: [
    { path: "parties.insured.name", value: "Amina", value_type: "string", verification_state: "extracted", confidence: 0.96, source_type: "extraction", has_citation: true },
    { path: "vehicle.reg", value: "KAA 111B", value_type: "string", verification_state: "extracted", confidence: 0.96, source_type: "extraction", has_citation: false },
    { path: "reserve.total", value: 123456n, value_type: "money", verification_state: "human_verified", confidence: null, source_type: "human", has_citation: true },
  ],
  documents: [{ id: "doc-1", filename: "claim-form.pdf", doc_type: "claim_form", status: "normalised", page_count: 2n, received_at: "2026-07-14T07:30:00Z" }],
  financials: [{ path: "reserve.total", amount_cents: 123456n, calc_run_id: "calc-1" }],
  timeline: [{ id: "event-1", type: "calc.completed", occurred_at: "2026-07-14T08:00:00Z", payload: { calc_run_id: "calc-1", result: "accepted" } }],
  systems: [{ system: "icon", status: "completed", event_type: "projection.completed" }],
  communications: [{ event_type: "email.received", subject: "Claim documents", thread_id: "thread-1" }],
  availability: {
    document_checklist: { status: "not_available", owner: "PRD-06" },
    systems: { status: "available", owner: "PRD-09" },
    communications: { status: "available", owner: "PRD-06" },
  },
};

function api(overrides: Partial<ConsoleApi> = {}): ConsoleApi {
  return {
    listReviews: vi.fn().mockResolvedValue([]),
    resolveReview: vi.fn(),
    getClaim360: vi.fn().mockResolvedValue(claim),
    getCitation: vi.fn().mockResolvedValue({ claim_id: "claim-1", field_path: "reserve.total", value: 123456n, value_type: "money", verification_state: "human_verified", document_id: "doc-1", page: 1, bbox: [0.1, 0.2, 0.4, 0.3], document_url: "/doc.pdf" }),
    ...overrides,
  };
}

describe("complete Claim 360", () => {
  it("renders operational detail across all seven tabs without raw payload dumps", async () => {
    render(<Claim360Page api={api()} claimId="claim-1" />);
    expect(await screen.findByText("Claim participants")).toBeVisible();
    expect(screen.getByText("96% confidence")).toBeVisible();

    await userEvent.click(screen.getByRole("tab", { name: "Documents" }));
    expect(screen.getByText("claim-form.pdf")).toBeVisible();
    expect(screen.getByText(/Checklist not available until PRD-06/)).toBeVisible();

    await userEvent.click(screen.getByRole("tab", { name: "Fields & Citations" }));
    await userEvent.click(screen.getAllByRole("button", { name: "Open citation" })[1]);
    expect(screen.getByText("Current value").parentElement).toHaveTextContent("KES 1,234.56");

    await userEvent.click(screen.getByRole("tab", { name: "Financials" }));
    await userEvent.click(screen.getByRole("button", { name: "Calc run calc-1" }));
    expect(screen.getByText("Events for calculation calc-1")).toBeVisible();
    expect(screen.getByText("Calc · Completed")).toBeVisible();

    await userEvent.click(screen.getByRole("tab", { name: "Systems" }));
    expect(screen.getByText("completed")).toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: "Communications" }));
    expect(screen.getByText("Claim documents")).toBeVisible();
    expect(screen.queryByRole("code")).not.toBeInTheDocument();
  });

  it("shows explicit authentication and authorisation recovery states", async () => {
    const authentication = api({ getClaim360: vi.fn().mockRejectedValue({ code: "INVALID_TOKEN", detail: "Expired" }) });
    const { rerender } = render(<Claim360Page api={authentication} claimId="claim-1" />);
    expect(await screen.findByText("Authentication required")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry" })).toBeVisible();

    rerender(<ReviewQueuePage api={api({ listReviews: vi.fn().mockRejectedValue({ code: "FORBIDDEN_ROLE", detail: "No queue role" }) })} />);
    expect(await screen.findByText("Access denied")).toBeVisible();
    expect(screen.getByText("FORBIDDEN_ROLE")).toBeVisible();
  });
});
