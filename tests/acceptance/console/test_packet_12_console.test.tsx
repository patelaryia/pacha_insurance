/** PACKET-12 protected browser acceptance. Builder must not modify this file.
 *
 * PRD-04 §4.3 S-3/S-4/S-5/S-6 per docs/packets/PACKET-12_ops_surfaces.md §3.1.
 * Producer-owned gaps (PRD-08 pack PDF, PRD-09 adapters, register #79 trend
 * windows, org-config identity management) must render explicit blocked
 * states — never fabricated data and never a silent blank.
 */
import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConsoleApi } from "../../../console/src/api/types";
import { AdminPage } from "../../../console/src/pages/AdminPage";
import { ApprovalsPage } from "../../../console/src/pages/ApprovalsPage";
import { PortfolioPage } from "../../../console/src/pages/PortfolioPage";
import { SlaBoardPage } from "../../../console/src/pages/SlaBoardPage";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const APPROVAL_ITEM = {
  id: "01HOPSPACKREVIEW0000000AAA",
  claim_id: "01HOPSCLAIM000000000000AAA",
  type: "PACK_REVIEW",
  subtype: null,
  status: "open",
  assigned_to: "user:01HOPSOFFICER000000000AAAA",
  payload: { capability_id: "pack.note_draft", output: { pack: "draft" } },
  workspace_layout: "pack_review",
  resolution_schema: "PACK_REVIEW@1",
  sla: [],
};

const TILES = [
  {
    series_id: "open_claims_by_state",
    status: "live",
    data: [{ state: "INTIMATED", count: 3 }],
  },
  {
    series_id: "sla_breaches",
    status: "live",
    data: [{ clock_id: "c1", claim_id: "01HOPSCLAIM000000000000AAA" }],
  },
  { series_id: "autonomy_rate_trend", status: "pending_capture", data: null },
];

const CLOCKS = [
  {
    clock_id: "01HOPSCLOCKNEAR0000000AAAA",
    claim_id: "01HOPSCLAIM000000000000AAA",
    definition_id: "sla.fixture_escalatable",
    state: "running",
    breach_at: "2026-07-15T05:00:00Z",
    escalate_to_role: "claims_manager",
  },
  {
    clock_id: "01HOPSCLOCKFAR00000000AAAA",
    claim_id: "01HOPSCLAIM000000000000BBB",
    definition_id: "sla.acknowledge",
    state: "running",
    breach_at: "2026-07-18T05:00:00Z",
    escalate_to_role: "pending_capture",
  },
];

function makeApi(overrides: Partial<Record<keyof ConsoleApi, unknown>> = {}) {
  return {
    listReviews: vi.fn(async () => ({ items: [APPROVAL_ITEM] })),
    getReview: vi.fn(async () => APPROVAL_ITEM),
    resolveReview: vi.fn(async () => ({ status: "resolved" })),
    getSlaBoard: vi.fn(async () => ({ clocks: CLOCKS })),
    escalateClocks: vi.fn(async (clockIds: string[]) => ({
      results: clockIds.map((clock_id, index) => ({
        clock_id,
        outcome: index === 0 ? "escalated" : "blocked_on_inputs",
      })),
    })),
    getPortfolio: vi.fn(async () => ({ tiles: TILES })),
    seriesCsvUrl: vi.fn(
      (seriesId: string) => `/console/ops/portfolio/${seriesId}.csv`,
    ),
    searchLedger: vi.fn(async () => ({
      rows: [
        {
          seq: 1,
          action: "claim.created",
          actor: "agent:intake",
          claim_id: "01HOPSCLAIM000000000000AAA",
          row_hash: "abc123",
        },
      ],
    })),
    getPacks: vi.fn(async () => ({
      packs: [{ id: "motor", version: "motor@1.0.0", entries: [] }],
    })),
    getCapabilities: vi.fn(async () => ({
      capabilities: [
        {
          id: "triage.route",
          current_level: "L0",
          max_level: "L3",
          pass_rate_window: 0,
          consecutive_approvals: 0,
          runs_to_promotion: null,
          sampling_rate: 100,
        },
      ],
    })),
    promoteCapability: vi.fn(async () => ({ code: "CRITERIA_NOT_MET" })),
    listNotifications: vi.fn(async () => ({ items: [] })),
    markNotificationRead: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ConsoleApi;
}

describe("S-3 Approval Workspace", () => {
  it("renders the band queue with the PRD-08 pack pane explicitly unavailable", async () => {
    const api = makeApi();
    render(<ApprovalsPage api={api} />);
    await waitFor(() => expect(screen.getByTestId("approval-queue")).toBeInTheDocument());
    expect(api.listReviews).toHaveBeenCalledWith(
      expect.objectContaining({ scope: "band" }),
    );
    const unavailable = screen.getByTestId("approval-pack-unavailable");
    expect(unavailable.textContent).toContain("PRD-08");
  });
});

describe("S-4 Portfolio Dashboard", () => {
  it("renders live tiles with CSV export and blocks uncaptured trend tiles", async () => {
    render(<PortfolioPage api={makeApi()} />);
    const liveTile = await screen.findByTestId("tile-open_claims_by_state");
    expect(liveTile.textContent).toContain("INTIMATED");

    const exportLink = screen.getByTestId("export-open_claims_by_state");
    expect(exportLink.getAttribute("href")).toContain(
      "/console/ops/portfolio/open_claims_by_state.csv",
    );

    const pending = screen.getByTestId("tile-autonomy_rate_trend");
    expect(pending.textContent).toContain("pending_capture");
    expect(
      screen.queryByTestId("export-autonomy_rate_trend"),
    ).not.toBeInTheDocument();
  });
});

describe("S-5 SLA Board", () => {
  it("renders clocks in server order and bulk escalates with visible blocked rows", async () => {
    const api = makeApi();
    render(<SlaBoardPage api={api} />);
    const near = await screen.findByTestId(`sla-row-${CLOCKS[0].clock_id}`);
    const far = screen.getByTestId(`sla-row-${CLOCKS[1].clock_id}`);
    expect(
      near.compareDocumentPosition(far) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(near.querySelector("input[type=checkbox]") as Element);
    fireEvent.click(far.querySelector("input[type=checkbox]") as Element);
    fireEvent.click(screen.getByTestId("sla-escalate"));

    await waitFor(() => expect(api.escalateClocks).toHaveBeenCalledTimes(1));
    expect(api.escalateClocks).toHaveBeenCalledWith([
      CLOCKS[0].clock_id,
      CLOCKS[1].clock_id,
    ]);
    const blocked = await screen.findByTestId(
      `sla-blocked-${CLOCKS[1].clock_id}`,
    );
    expect(blocked.textContent).toContain("blocked_on_inputs");
  });
});

describe("S-6 Admin", () => {
  it("renders packs, capabilities, and explicit unavailable/read-only markers", async () => {
    render(<AdminPage api={makeApi()} />);
    const packRow = await screen.findByTestId("pack-version-row");
    expect(packRow.textContent).toContain("motor@1.0.0");

    const capability = screen.getByTestId("capability-row-triage.route");
    expect(capability.textContent).toContain("L0");

    const adapters = screen.getByTestId("adapter-health-unavailable");
    expect(adapters.textContent).toContain("PRD-09");

    const identity = screen.getByTestId("user-role-readonly");
    expect(identity.textContent?.toLowerCase()).toContain("config");
  });

  it("ledger search calls the API and renders hash-chained rows", async () => {
    const api = makeApi();
    render(<AdminPage api={api} />);
    const input = await screen.findByTestId("ledger-search-input");
    fireEvent.change(input, { target: { value: "claim.created" } });
    fireEvent.submit(input.closest("form") as Element);

    await waitFor(() => expect(api.searchLedger).toHaveBeenCalled());
    const row = await screen.findByTestId("ledger-row-1");
    expect(row.textContent).toContain("claim.created");
    expect(row.textContent).toContain("abc123");
  });
});
