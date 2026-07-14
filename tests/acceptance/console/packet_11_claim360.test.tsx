/**
 * PACKET-11 acceptance (vitest) — PRD-04 §4.3 S-2 Claim 360: status rail,
 * DECLINED banner (structurally impossible to miss), EAT rendering (ED-2),
 * money display from integer cents (ED-8 stays backend-only).
 *
 * Protected (CODEOWNERS): the builder may not modify this file.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";

import { Claim360 } from "@console/screens/Claim360";

const CLAIM_ID = "01HCLAIM000000000000000AAA";

const FSM = {
  primary_path: [
    "INTIMATED", "TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT",
    "REPORT_RECEIVED", "REGISTERED", "RESERVED", "PACK_READY",
    "IN_APPROVAL", "APPROVED", "IN_REPAIR", "REINSPECTION", "RELEASED",
    "SETTLEMENT", "SETTLED", "CLOSED",
  ],
  write_off_path: [
    "REPORT_RECEIVED", "WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION",
  ],
  terminal: ["CLOSED", "DECLINED", "WITHDRAWN", "VOID"],
};

function makeApi(status: string) {
  return {
    listReviews: vi.fn(async () => ({ items: [] })),
    getReview: vi.fn(async () => null),
    resolveReview: vi.fn(async () => ({})),
    getClaim: vi.fn(async () => ({
      id: CLAIM_ID,
      status,
      substatus: null,
      blocked_reasons: [],
      fields: {
        "assessment.agreed_quote": {
          value: 1234500,
          value_type: "money",
          verification_state: "human_verified",
          confidence: null,
          source_type: "human",
        },
      },
    })),
    getTimeline: vi.fn(async () => ({
      events: [
        {
          id: "01HEVENT000000000000000AAA",
          type: "claim.created",
          created_at: "2026-07-14T09:00:00Z",
          actor: "agent:intake",
          payload: {},
        },
      ],
    })),
    listDocuments: vi.fn(async () => ({ documents: [] })),
    getFinancials: vi.fn(async () => ({ runs: [] })),
    getFsmStates: vi.fn(async () => FSM),
    getDocumentBlobUrl: vi.fn((id: string) => `/api/console/documents/${id}/blob`),
  };
}

async function renderClaim(status: string) {
  const api = makeApi(status);
  render(<Claim360 api={api as never} claimId={CLAIM_ID} />);
  await waitFor(() => expect(screen.getByTestId("status-rail")).toBeTruthy());
  return api;
}

describe("S-2 Claim 360 (PRD-04 §4.3 verbatim)", () => {
  it("renders the status rail in primary-path order with the current step marked", async () => {
    await renderClaim("REGISTERED");
    const rail = screen.getByTestId("status-rail");
    const steps = FSM.primary_path.map(
      (state) => screen.getByTestId(`status-step-${state}`),
    );
    // document order must match the primary path order
    for (let i = 1; i < steps.length; i += 1) {
      const relation = steps[i - 1].compareDocumentPosition(steps[i]);
      expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(rail.contains(steps[0])).toBe(true);
    expect(
      screen.getByTestId("status-step-REGISTERED").getAttribute("aria-current"),
    ).toBe("step");
  });

  it("DECLINED renders a full-width alert banner with a blocked reopen action", async () => {
    await renderClaim("DECLINED");
    const banner = screen.getByTestId("declined-banner");
    expect(banner.getAttribute("role")).toBe("alert");
    const reopen = screen.getByTestId("reopen-action") as HTMLButtonElement;
    expect(reopen.disabled).toBe(true);
    expect(reopen.getAttribute("data-blocked-reason")).toBe("blocked_on_inputs");
  });

  it("renders timeline timestamps in EAT (UTC+3) regardless of host timezone", async () => {
    await renderClaim("REGISTERED");
    const entry = await screen.findByTestId(
      "timeline-entry-01HEVENT000000000000000AAA",
    );
    expect(entry.textContent).toContain("12:00");
    expect(entry.textContent).toContain("EAT");
  });

  it("formats money fields from integer cents", async () => {
    await renderClaim("REGISTERED");
    const money = await screen.findByTestId(
      "field-money-assessment.agreed_quote",
    );
    expect(money.textContent).toContain("KES");
    expect(money.textContent).toContain("12,345.00");
  });
});
