/**
 * PACKET-21 acceptance — console (§18).
 *
 * Protected (CODEOWNERS): the builder may not weaken this file once merged.
 *
 * Every fixture here is synthetic. No target value, selector, credential, or
 * raw blob key may reach the DOM, and a completed state is only ever rendered
 * from a reconciled server response.
 */
import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectionSystems } from "../../../console/src/components/ProjectionSystems";
import { AdminPage } from "../../../console/src/pages/AdminPage";
import { Claim360Page } from "../../../console/src/pages/Claim360Page";
import { Workspace } from "../../../console/src/workspaces/registry";
import type {
  AdapterHealthRow,
  ConsoleApi,
  ProjectionRpaView,
  ProjectionSurface,
  ReviewItem,
} from "../../../console/src/api/types";

afterEach(cleanup);

const CLAIM = "01HP21CLAIM0000000000AAAAA";
const PROJECTION = "01HP21PROJECTION00000AAAAA";
const REVIEW = "01HP21REVIEW000000000AAAAA";
const OPERATION = "edms.claims_workflow";
const CAPABILITY = "project.edms.claims_workflow";
const SNAPSHOT = "a9870923b966aff4df9d9e7878cbb726a369b87399c42a789ca441483ab9750a";

const SURFACE: ProjectionSurface = {
  operations: [
    {
      id: OPERATION,
      capability_id: CAPABILITY,
      system: "edms",
      mode: "rpa",
      status: "live",
      blocked_on: null,
      owner_prd: "PRD-09",
      version: "1.0.0",
    },
  ],
  projections: [
    {
      id: PROJECTION,
      claim_id: CLAIM,
      operation: OPERATION,
      capability_id: CAPABILITY,
      mode: "rpa",
      status: "executing",
      snapshot_hash: SNAPSHOT,
      definition_version: "1.0.0",
      blocked_on: null,
      readback_paths: ["external.edms.folder_ref"],
      attested_by: null,
      attested_at: null,
      paste_seconds: null,
      started_at: null,
      groups_done: {},
      created_at: "2026-07-23T08:00:00+00:00",
      completed_at: null,
    },
  ],
};

function rpaView(overrides: Partial<ProjectionRpaView> = {}): ProjectionRpaView {
  return {
    projection_id: PROJECTION,
    claim_id: CLAIM,
    operation: OPERATION,
    capability_id: CAPABILITY,
    definition_version: "1.0.0",
    snapshot_hash: SNAPSHOT,
    mode: "rpa",
    status: "executing",
    substate: "running",
    gate: { state: "authorised", review_id: null },
    run_id: "01HP21RUN00000000000AAAAA",
    attempt: 1,
    attempts: [],
    lease: {
      runner_id: "runner-fixture",
      expires_at: "2026-07-23T08:02:00+00:00",
      healthy: true,
    },
    current_step: "s3",
    evidence: [
      {
        evidence_id: "ev-1",
        step_id: "s1",
        phase: "before",
        sha256: "aa".repeat(32),
        captured_at: "2026-07-23T08:00:01+00:00",
        attempt: 1,
        url: `/console/claims/${CLAIM}/projections/${PROJECTION}/evidence/ev-1`,
      },
    ],
    reconciliation: { status: "pending", detected_by: null, mismatch_paths: [] },
    circuit: { status: "closed", reason_code: null, definition_version: null },
    fallback: null,
    terminal: null,
    ...overrides,
  };
}

function api(overrides: Partial<ConsoleApi> = {}): ConsoleApi {
  return {
    listReviews: vi.fn().mockResolvedValue([]),
    resolveReview: vi.fn().mockResolvedValue(undefined),
    getClaim360: vi.fn().mockResolvedValue({
      claim: {
        id: CLAIM,
        status: "REGISTERED",
        substatus: null,
        assigned_to: null,
        created_at: "2026-07-23T08:00:00Z",
        updated_at: "2026-07-23T08:00:00Z",
      },
      header: { insured: null, registration: null, amount_cents: null },
      fields: [],
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
    }),
    getCitation: vi.fn(),
    getProjections: vi.fn().mockResolvedValue(SURFACE),
    getProjectionRpa: vi.fn().mockResolvedValue(rpaView()),
    ...overrides,
  } as unknown as ConsoleApi;
}

function reviewItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: REVIEW,
    claim_id: CLAIM,
    type: "DRAFT_RELEASE",
    subtype: "projection_rpa",
    status: "open",
    workspace_layout: "projection_rpa_release",
    resolution_schema: "DRAFT_RELEASE_PROJECTION@1",
    resolution_actions: ["approve", "edit_approve", "reject"],
    payload: {
      capability_id: CAPABILITY,
      agent_run_id: "01HP21RUN00000000000AAAAA",
      action: {
        type: "projection.rpa.execute",
        payload: {
          projection_id: PROJECTION,
          operation: OPERATION,
          definition_version: "1.0.0",
          snapshot_hash: SNAPSHOT,
        },
      },
    },
    created_at: "2026-07-23T08:00:00+00:00",
    ...overrides,
  } as unknown as ReviewItem;
}

// --- 1. the seven Claim-360 tabs are unchanged ----------------------------------------

describe("Claim-360 tabs", () => {
  it("still shows exactly the seven PRD-04 tabs", async () => {
    render(<Claim360Page api={api()} claimId={CLAIM} />);
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual([
      "Overview",
      "Documents",
      "Fields & Citations",
      "Financials",
      "Timeline",
      "Systems",
      "Communications",
    ]);
  });
});

// --- 2/3/4. run states, evidence order, and no optimistic completion -------------------

describe("Systems robotic run", () => {
  it("renders each substate from the server without a page reload", async () => {
    const client = api();
    render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    expect(await screen.findByTestId("rpa-substate")).toHaveTextContent("Running");

    const frames = within(screen.getByTestId("rpa-evidence")).getAllByRole("listitem");
    expect(frames).toHaveLength(1);
    expect(frames[0].textContent).toContain("Step s1");
    // Ordered, keyboard reachable, and never a raw blob key.
    expect(screen.getByTestId("rpa-evidence").tagName).toBe("OL");
    expect(frames[0].querySelector("a")?.getAttribute("href")).toContain("/evidence/ev-1");
    expect(document.body.innerHTML).not.toContain("projection-evidence/");

    (client.getProjectionRpa as ReturnType<typeof vi.fn>).mockResolvedValue(
      rpaView({
        substate: "reconciling",
        status: "verifying",
        evidence: [
          ...rpaView().evidence,
          {
            evidence_id: "ev-2",
            step_id: "s1",
            phase: "after",
            sha256: "bb".repeat(32),
            captured_at: "2026-07-23T08:00:02+00:00",
            attempt: 1,
            url: `/console/claims/${CLAIM}/projections/${PROJECTION}/evidence/ev-2`,
          },
        ],
      }),
    );
    fireEvent.click(screen.getByTestId("rpa-refresh"));
    await waitFor(() =>
      expect(screen.getByTestId("rpa-substate")).toHaveTextContent("Reconciling"));
    expect(
      within(screen.getByTestId("rpa-evidence")).getAllByRole("listitem"),
    ).toHaveLength(2);
  });

  it("never renders completion optimistically and recovers from a stale read", async () => {
    const client = api({
      getProjectionRpa: vi.fn().mockRejectedValueOnce({
        code: "PROJECTION_LEASE_STALE",
        detail: "That lease has expired",
      }).mockResolvedValue(rpaView({ substate: "fallback_to_paste", status: "queued" })),
    });
    render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    expect(await screen.findByRole("alert")).toHaveTextContent("PROJECTION_LEASE_STALE");
    expect(screen.queryByTestId("rpa-substate")).toBeNull();

    fireEvent.click(screen.getByTestId(`open-rpa-${PROJECTION}`));
    expect(await screen.findByTestId("rpa-substate")).toHaveTextContent("Fallback to paste");
  });

  it("shows a loud uncertain-write state with no retry and no paste offer", async () => {
    const client = api({
      getProjectionRpa: vi.fn().mockResolvedValue(
        rpaView({
          substate: "failed",
          status: "failed",
          terminal: { subtype: "uncertain_write", reason_code: "ui_drift_after_possible_write" },
        }),
      ),
    });
    render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    const alert = await screen.findByTestId("rpa-uncertain-write");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert.textContent).toContain("no retry");
    expect(screen.queryByText(/Switch this row to paste/)).toBeNull();
  });

  it("links an awaiting-confirmation run to its review and sends nothing", async () => {
    const client = api({
      getProjectionRpa: vi.fn().mockResolvedValue(
        rpaView({
          substate: "awaiting_confirmation",
          status: "queued",
          gate: { state: "staged", review_id: REVIEW },
        }),
      ),
    });
    render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    const notice = await screen.findByTestId("rpa-awaiting-confirmation");
    expect(notice.textContent).toContain("Nothing has been sent");
    expect(notice.querySelector("a")?.getAttribute("href")).toBe(`/reviews/${REVIEW}`);
  });
});

// --- 5. divergence values require the authorised workspace ----------------------------

describe("Divergence", () => {
  it("keeps expected and actual values out of the Systems list DOM", async () => {
    const client = api({
      getProjectionRpa: vi.fn().mockResolvedValue(
        rpaView({
          substate: "diverged",
          status: "diverged",
          reconciliation: {
            status: "diverged",
            detected_by: "rpa_readback",
            mismatch_paths: [
              {
                path: "parties.insured.name",
                kind: "text",
                expected_sha256: "cc".repeat(32),
                actual_sha256: "dd".repeat(32),
                evidence_ids: ["ev-1"],
              },
            ],
          },
        }),
      ),
    });
    render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    const alert = await screen.findByTestId("rpa-diverged");
    expect(alert).toHaveAttribute("role", "alert");
    expect(screen.getByTestId("rpa-mismatch-paths").textContent)
      .toContain("parties.insured.name");
    // Hashes only — no value, and nothing hidden in a data attribute.
    expect(document.body.innerHTML).not.toContain("Grace");
    expect(document.body.innerHTML).not.toContain("data-expected");
  });

  it("records exactly one disposition and corrects neither side", async () => {
    const client = api();
    render(
      <Workspace
        item={reviewItem({
          type: "EXCEPTION",
          subtype: "divergence",
          workspace_layout: "projection_divergence",
          resolution_schema: "EXCEPTION_DIVERGENCE@1",
          payload: {
            capability_id: CAPABILITY,
            detected_by: "rpa_readback",
            paths: [
              {
                path: "reserve.total",
                kind: "money",
                expected_sha256: "cc".repeat(32),
                actual_sha256: "dd".repeat(32),
                evidence_ids: [],
              },
            ],
          },
        })}
        api={client}
      />,
    );
    expect(screen.getByTestId("divergence-path-reserve.total")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("divergence-disposition"), {
      target: { value: "platform_snapshot_wrong" },
    });
    fireEvent.click(screen.getByTestId("divergence-resolve"));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(1));
    expect(client.resolveReview).toHaveBeenCalledWith(REVIEW, {
      action: "approve",
      schema_version: "EXCEPTION_DIVERGENCE@1",
      payload: {
        capability_id: CAPABILITY,
        disposition: "platform_snapshot_wrong",
        diff: { typed_changes: [], prose_change_ratio: 0 },
      },
    });
  });
});

// --- 6. sampled paste readback ---------------------------------------------------------

describe("Sampled paste readback", () => {
  function pasteItem() {
    return reviewItem({
      type: "PASTE_READBACK_CHECK",
      subtype: null,
      workspace_layout: "paste_readback",
      resolution_schema: "PASTE_READBACK_CHECK@2",
      payload: {
        capability_id: CAPABILITY,
        projection_id: PROJECTION,
        readback_paths: ["external.edms.folder_ref"],
      },
    });
  }

  it("captures, then approves only on an exact server comparison", async () => {
    const client = api({
      capturePasteReadback: vi.fn().mockResolvedValue({
        capture_id: "cap-1",
        mismatch_paths: [],
        hashes: {},
        evidence_id: null,
      }),
    });
    render(<Workspace item={pasteItem()} api={client} />);
    fireEvent.change(screen.getByTestId("paste-readback-external.edms.folder_ref"), {
      target: { value: "EDMS/2026/004521" },
    });
    fireEvent.click(screen.getByTestId("paste-readback-capture"));
    await screen.findByTestId("paste-readback-result");
    expect(client.capturePasteReadback).toHaveBeenCalledWith(REVIEW, {
      "external.edms.folder_ref": "EDMS/2026/004521",
    });
    expect(screen.getByTestId("paste-readback-diverge")).toBeDisabled();

    fireEvent.click(screen.getByTestId("paste-readback-approve"));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(1));
    expect(
      (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0][1].payload.capture_id,
    ).toBe("cap-1");
  });

  it("offers only the divergence action when the server found a mismatch", async () => {
    const client = api({
      capturePasteReadback: vi.fn().mockResolvedValue({
        capture_id: "cap-2",
        mismatch_paths: ["external.edms.folder_ref"],
        hashes: {},
        evidence_id: null,
      }),
    });
    render(<Workspace item={pasteItem()} api={client} />);
    fireEvent.change(screen.getByTestId("paste-readback-external.edms.folder_ref"), {
      target: { value: "EDMS/2026/999999" },
    });
    fireEvent.click(screen.getByTestId("paste-readback-capture"));
    await screen.findByTestId("paste-readback-result");
    expect(screen.getByTestId("paste-readback-approve")).toBeDisabled();
    fireEvent.click(screen.getByTestId("paste-readback-diverge"));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(1));
    const call = (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(call.action).toBe("edit_approve");
    expect(call.payload.diff.typed_changes).toEqual([
      { path: "external.edms.folder_ref", kind: "text" },
    ]);
  });

  it("requires a reason to reject and invents no match", async () => {
    const client = api({ capturePasteReadback: vi.fn() });
    render(<Workspace item={pasteItem()} api={client} />);
    expect(screen.getByTestId("paste-readback-reject")).toBeDisabled();
    fireEvent.change(screen.getByTestId("paste-readback-reason"), {
      target: { value: "EDMS is unavailable" },
    });
    fireEvent.click(screen.getByTestId("paste-readback-reject"));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(1));
    expect(
      (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0][1].action,
    ).toBe("reject");
  });
});

// --- 7/8. S-6 adapter health and circuit reset ----------------------------------------

const ADAPTERS: AdapterHealthRow[] = [
  {
    system: "icon",
    configured_mode: "paste_assist",
    effective_mode: "paste_assist",
    status: "unavailable",
    reason_code: "pending_capture",
    runner_last_seen_at: null,
    circuit_operation_ids: [],
  },
  {
    system: "edms",
    configured_mode: "rpa",
    effective_mode: "paste_assist",
    status: "circuit_open",
    reason_code: "ui_drift",
    runner_last_seen_at: "2026-07-23T08:00:00+00:00",
    circuit_operation_ids: [OPERATION],
  },
];

function adminApi(overrides: Partial<ConsoleApi> = {}): ConsoleApi {
  return {
    getPacks: vi.fn().mockResolvedValue({
      packs: [{ id: "motor", version: "motor@1.0.0", entries: [] }],
      adapter_health: ADAPTERS,
    }),
    getCapabilities: vi.fn().mockResolvedValue({ capabilities: [] }),
    searchLedger: vi.fn().mockResolvedValue({ rows: [] }),
    ...overrides,
  } as unknown as ConsoleApi;
}

describe("S-6 adapter health", () => {
  it("replaces the unavailable placeholder without exposing a secret or selector", async () => {
    render(<AdminPage api={adminApi()} />);
    await screen.findByTestId("adapter-health");
    expect(screen.queryByTestId("adapter-health-unavailable")).toBeNull();
    expect(screen.getByTestId("adapter-row-icon").textContent).toContain("pending capture");
    expect(screen.getByTestId("adapter-row-edms").textContent).toContain("circuit open");
    const markup = document.body.innerHTML;
    for (const forbidden of ["secret_ref", "base_url", "#policyNo", "password"]) {
      expect(markup).not.toContain(forbidden);
    }
  });

  it("keeps the honest unavailable card when the application has no PRD-09", async () => {
    render(
      <AdminPage
        api={adminApi({
          getPacks: vi.fn().mockResolvedValue({
            packs: [],
            adapter_health: { status: "unavailable", owner: "PRD-09" },
          }),
        })}
      />,
    );
    expect(await screen.findByTestId("adapter-health-unavailable")).toHaveTextContent("PRD-09");
  });

  it("surfaces a refused circuit reset rather than pretending it cleared", async () => {
    const clear = vi.fn().mockRejectedValue({ code: "PROJECTION_CIRCUIT_BLOCKED" });
    render(<AdminPage api={adminApi({ clearProjectionCircuit: clear })} />);
    fireEvent.click(await screen.findByTestId(`clear-circuit-${OPERATION}`));
    await waitFor(() => expect(clear).toHaveBeenCalledWith(OPERATION));
    expect(await screen.findByRole("alert")).toHaveTextContent("not qualified for reset");
  });
});

// --- 9/10. accessibility and a usable 1366x768 layout ----------------------------------

describe("Accessibility", () => {
  it("passes axe and stays within a 1366px viewport", async () => {
    const client = api({
      getProjectionRpa: vi.fn().mockResolvedValue(rpaView({ substate: "diverged" })),
    });
    const { container } = render(<ProjectionSystems api={client} claimId={CLAIM} />);
    fireEvent.click(await screen.findByTestId(`open-rpa-${PROJECTION}`));
    await screen.findByTestId("rpa-panel");
    const results = await axe.run(container, {
      rules: { region: { enabled: false }, "color-contrast": { enabled: false } },
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
    for (const element of Array.from(container.querySelectorAll("table"))) {
      expect(element.closest(".table-scroll, ol, ul")).not.toBeNull();
    }
  });
});
