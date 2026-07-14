import "@testing-library/jest-dom/vitest";

import axe from "axe-core";
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Claim360, ConsoleApi, ReviewItem } from "./api/types";
import { Claim360Page } from "./pages/Claim360Page";
import { ReviewQueuePage } from "./pages/ReviewQueuePage";

afterEach(cleanup);

const review: ReviewItem = {
  id: "review-a11y",
  claim_id: "claim-a11y",
  type: "FIELD_VERIFY",
  subtype: null,
  status: "open",
  assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
  payload: {
    path: "vehicle.reg",
    candidate_value: "KAA 111B",
    capability_id: "doc.extract",
    value_type: "string",
  },
  workspace_layout: "field_verify",
  resolution_schema: "FIELD_VERIFY@1",
  sla: [{ state: "running" }],
};

const claim: Claim360 = {
  claim: {
    id: "claim-a11y",
    status: "RESERVED",
    substatus: null,
    assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
    created_at: "2026-07-14T07:00:00Z",
    updated_at: "2026-07-14T08:00:00Z",
  },
  header: { insured: "Amina", registration: "KAA 111B", amount_cents: 123_400n },
  fields: [{
    path: "vehicle.reg",
    value: "KAA 111B",
    value_type: "string",
    verification_state: "human_verified",
    confidence: null,
    source_type: "human",
    has_citation: false,
  }],
  documents: [],
  financials: [],
  timeline: [],
  systems: [],
  communications: [],
  availability: {
    document_checklist: { status: "not_available", owner: "PRD-06" },
    systems: { status: "not_available", owner: "PRD-09" },
    communications: { status: "not_available", owner: "PRD-06" },
  },
};

function api(): ConsoleApi {
  return {
    listReviews: vi.fn().mockResolvedValue([review]),
    resolveReview: vi.fn().mockResolvedValue(undefined),
    getClaim360: vi.fn().mockResolvedValue(claim),
    getCitation: vi.fn(),
  };
}

async function expectNoAxeViolations(container: HTMLElement) {
  const result = await axe.run(container, {
    // jsdom has no layout/canvas implementation; colour contrast remains a live-browser gate.
    rules: { "color-contrast": { enabled: false } },
  });
  expect(
    result.violations.map(({ id, nodes }) => ({
      id,
      targets: nodes.map((node) => node.target),
    })),
  ).toEqual([]);
}

describe("Packet 11 automated accessibility gate", () => {
  it("finds no axe violations in the loaded review queue", async () => {
    const { container } = render(<ReviewQueuePage api={api()} />);
    await screen.findByRole("option", { name: /FIELD_VERIFY/ });
    await expectNoAxeViolations(container);
  });

  it("finds no axe violations in Claim 360", async () => {
    const { container } = render(<Claim360Page api={api()} claimId="claim-a11y" />);
    await screen.findByRole("heading", { name: "claim-a11y" });
    await expectNoAxeViolations(container);
  });
});
