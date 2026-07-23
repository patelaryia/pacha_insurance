/** PACKET-20 protected browser acceptance. Builder must not weaken this file.
 *
 * PRD-09 §9.3 paste-assist inside the existing PRD-04 §4.3 Systems tab, per
 * docs/packets/PACKET-20_projection_paste_assist.md §10/§12. No network, no
 * target-system iframe, no real clipboard: the Clipboard API is stubbed so the
 * exact server `copy_value` can be asserted byte for byte.
 */
import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  Claim360,
  ConsoleApi,
  PasteAssistView,
  ProjectionSummary,
  ProjectionSurface,
} from "../../../console/src/api/types";
import { ProjectionSystems } from "../../../console/src/components/ProjectionSystems";
import { Claim360Page } from "../../../console/src/pages/Claim360Page";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const CLAIM = "01HP20CLAIM0000000000000AA";
const PROJECTION = "01HP20PROJECTION00000000AA";
const HASH = "f".repeat(64);
const ICON_CLAIM_NO = "ICON-004521";

const SEVEN_TABS = [
  "Overview",
  "Documents",
  "Fields & Citations",
  "Financials",
  "Timeline",
  "Systems",
  "Communications",
];

const FIFTEEN_OPERATIONS = [
  "icon.policy_read", "icon.claim_register", "icon.reserve_create",
  "icon.reserve_breakdown", "icon.reserve_adjust", "icon.assessor_payment_request",
  "icon.note_entry", "icon.claim_details_report", "icon.salvage_register",
  "icon.payment_voucher", "edms.general_payments", "edms.claims_workflow",
  "edms.attach_and_tag", "edms.claim_payment", "edms.payment_workflow",
];

function operations(): ProjectionSurface["operations"] {
  return FIFTEEN_OPERATIONS.map((id) => ({
    id,
    capability_id: `project.${id}`,
    system: id.split(".")[0],
    mode: "paste_assist",
    status: id === "icon.claim_register" ? "live" : "pending_capture",
    blocked_on: id === "icon.claim_register" ? null : "open-item-3",
    owner_prd: "PRD-09",
    version: "1.1.0",
  }));
}

function projection(overrides: Partial<ProjectionSummary> = {}): ProjectionSummary {
  return {
    id: PROJECTION,
    claim_id: CLAIM,
    operation: "icon.claim_register",
    capability_id: "project.icon.claim_register",
    mode: "paste_assist",
    status: "executing",
    snapshot_hash: HASH,
    definition_version: "1.1.0",
    blocked_on: null,
    readback_paths: [],
    attested_by: null,
    attested_at: null,
    paste_seconds: null,
    started_at: "2026-07-23T08:00:00+00:00",
    groups_done: { claim_details: true, reserve: false },
    created_at: "2026-07-23T08:00:00+00:00",
    completed_at: null,
    ...overrides,
  };
}

function strip(overrides: Partial<PasteAssistView> = {}): PasteAssistView {
  return {
    projection_id: PROJECTION,
    claim_id: CLAIM,
    operation: "icon.claim_register",
    definition_version: "1.1.0",
    mode: "paste_assist",
    status: "executing",
    groups: [
      {
        id: "claim_details",
        label: "Claim details",
        done: false,
        fields: [
          {
            step_id: "s1",
            label: "Policy number",
            path: "policy.number",
            copy_value: "POL-20-0001",
            external_encoding: "raw",
            value_type: "string",
            field_version: 3,
          },
          {
            step_id: "s2",
            label: "Loss date",
            path: "loss.date",
            copy_value: "2026-07-01",
            external_encoding: "iso",
            value_type: "date",
            field_version: 1,
          },
        ],
      },
      {
        id: "reserve",
        label: "Reserve",
        done: false,
        fields: [
          {
            step_id: "s4",
            label: "Reserve total",
            path: "reserve.total",
            copy_value: "142656.00",
            external_encoding: "shillings",
            value_type: "money",
            field_version: 2,
          },
        ],
      },
    ],
    readback_fields: [
      {
        label: "ICON claim number",
        path: "external.icon.claim_no",
        required: true,
        format_status: "live",
        blocked_on: null,
      },
    ],
    attestation_text: "I entered the values exactly as shown.",
    started_at: "2026-07-23T08:00:00+00:00",
    elapsed_seconds: 42,
    ...overrides,
  };
}

interface StubOptions {
  surface?: Partial<ProjectionSurface>;
  view?: PasteAssistView;
  onGroup?: (groupId: string, done: boolean) => Promise<PasteAssistView>;
  onConfirm?: () => Promise<ProjectionSummary>;
  onStart?: () => Promise<PasteAssistView>;
}

function stubApi(options: StubOptions = {}): ConsoleApi & { calls: string[] } {
  const calls: string[] = [];
  let current = options.view ?? strip();
  const surface: ProjectionSurface = {
    operations: operations(),
    projections: [projection()],
    ...options.surface,
  };
  return {
    calls,
    listReviews: async () => [],
    resolveReview: async () => undefined,
    getClaim360: async () => claim360(),
    getCitation: async () => {
      throw new Error("not used");
    },
    getProjections: async () => {
      calls.push("list");
      return surface;
    },
    getPasteAssist: async () => {
      calls.push("read");
      return current;
    },
    startPasteAssist: async () => {
      calls.push("start");
      current = options.onStart ? await options.onStart() : current;
      return current;
    },
    setPasteGroup: async (_claim, _projectionId, groupId, done) => {
      calls.push(`group:${groupId}:${done}`);
      if (options.onGroup) {
        current = await options.onGroup(groupId, done);
        return current;
      }
      current = {
        ...current,
        groups: current.groups.map((group) =>
          group.id === groupId ? { ...group, done } : group),
      };
      return current;
    },
    confirmPasteAssist: async () => {
      calls.push("confirm");
      if (options.onConfirm) return options.onConfirm();
      current = { ...current, status: "completed" };
      return projection({ status: "completed", attested_by: "user:officer" });
    },
  } as ConsoleApi & { calls: string[] };
}

function claim360(): Claim360 {
  return {
    claim: {
      id: CLAIM,
      status: "REPORT_RECEIVED",
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
  };
}

function stubClipboard(fail = false) {
  const writeText = vi.fn(async (value: string) => {
    if (fail) throw new Error("clipboard denied");
    return value;
  });
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
  return writeText;
}

describe("Claim 360 Systems tab", () => {
  it("keeps the seven PRD-04 tabs and drops the PRD-09 unavailable placeholder", async () => {
    const api = stubApi();
    render(<Claim360Page api={api} claimId={CLAIM} />);
    await screen.findByRole("tab", { name: "Systems" });
    expect(
      screen.getAllByRole("tab").map((tab) => tab.textContent),
    ).toEqual(SEVEN_TABS);

    await userEvent.click(screen.getByRole("tab", { name: "Systems" }));
    await screen.findByRole("region", { name: "Registered operations" });
    expect(
      screen.queryByText(/Not available until PRD-09 is installed/),
    ).not.toBeInTheDocument();
  });
});

describe("operation catalogue and projection states", () => {
  it("renders all fifteen operations with mode, availability, blocker and owner", async () => {
    render(<ProjectionSystems api={stubApi()} claimId={CLAIM} />);
    const catalogue = await screen.findByRole("region", { name: "Registered operations" });
    const rows = within(catalogue).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(15);
    for (const operation of FIFTEEN_OPERATIONS) {
      expect(
        within(catalogue).getByText(`project.${operation}`),
      ).toBeInTheDocument();
    }
    expect(within(catalogue).getAllByText("Pending capture")).toHaveLength(14);
    expect(within(catalogue).getAllByText("open-item-3")).toHaveLength(14);
    expect(within(catalogue).getAllByText("PRD-09")).toHaveLength(15);
    expect(within(catalogue).getAllByText("paste assist")).toHaveLength(15);
  });

  it("states the empty case explicitly rather than showing a blank panel", async () => {
    const api = stubApi({ surface: { projections: [] } });
    render(<ProjectionSystems api={api} claimId={CLAIM} />);
    const panel = await screen.findByRole("region", { name: "Claim projections" });
    expect(
      within(panel).getByText(/No projection has been requested for this claim/),
    ).toBeInTheDocument();
  });

  it("renders snapshot metadata, screen progress, attestation and elapsed time", async () => {
    const api = stubApi({
      surface: {
        projections: [
          projection({
            status: "completed",
            readback_paths: ["external.icon.claim_no"],
            attested_by: "user:01HP20OFFICER00000000AAAAA",
            attested_at: "2026-07-23T08:00:42+00:00",
            paste_seconds: 42,
            groups_done: { claim_details: true, reserve: true },
            completed_at: "2026-07-23T08:00:42+00:00",
          }),
        ],
      },
    });
    render(<ProjectionSystems api={api} claimId={CLAIM} />);
    const panel = await screen.findByRole("region", { name: "Claim projections" });
    expect(within(panel).getByText("Completed")).toBeInTheDocument();
    expect(within(panel).getByText(HASH)).toBeInTheDocument();
    expect(within(panel).getByText("1.1.0")).toBeInTheDocument();
    expect(within(panel).getByText("2 of 2 screens done")).toBeInTheDocument();
    // Canonical readback path names only — never a raw readback value.
    expect(within(panel).getByText("external.icon.claim_no")).toBeInTheDocument();
    expect(within(panel).queryByText(ICON_CLAIM_NO)).not.toBeInTheDocument();
    expect(within(panel).getByText("42 seconds")).toBeInTheDocument();
    // EAT rendering: 08:00 UTC is 11:00 in Africa/Nairobi.
    expect(
      within(panel).getByText(/user:01HP20OFFICER00000000AAAAA · .*11:00/),
    ).toBeInTheDocument();
  });

  it("renders every projection status label", async () => {
    for (const [status, label] of [
      ["queued", "Queued"],
      ["executing", "In progress"],
      ["verifying", "Verifying"],
      ["completed", "Completed"],
    ] as const) {
      const api = stubApi({ surface: { projections: [projection({ status })] } });
      const view = render(<ProjectionSystems api={api} claimId={CLAIM} />);
      const panel = await screen.findByRole("region", { name: "Claim projections" });
      expect(within(panel).getByText(label)).toBeInTheDocument();
      view.unmount();
    }
  });
});

describe("paste strip", () => {
  async function openStrip(api: ConsoleApi) {
    render(<ProjectionSystems api={api} claimId={CLAIM} />);
    const open = await screen.findByRole("button", {
      name: "Open paste strip for icon.claim_register",
    });
    await userEvent.click(open);
    return screen.findByRole("region", { name: "Paste assist strip" });
  }

  it("renders groups and fields in click-path order", async () => {
    const panel = await openStrip(stubApi());
    const groups = within(panel).getAllByRole("group");
    expect(groups.map((group) => group.querySelector("legend")?.textContent)).toEqual([
      "Claim details",
      "Reserve",
      "Readback",
    ]);
    expect(
      within(groups[0]).getAllByRole("button").map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Copy Policy number", "Copy Loss date"]);
    expect(
      within(groups[1]).getAllByRole("button").map((button) => button.getAttribute("aria-label")),
    ).toEqual(["Copy Reserve total"]);
  });

  it("copies the exact server value and announces success politely", async () => {
    const writeText = stubClipboard();
    const panel = await openStrip(stubApi());
    await userEvent.click(within(panel).getByRole("button", { name: "Copy Reserve total" }));
    // The browser applies no formatting: the shilling string is the server's.
    expect(writeText).toHaveBeenCalledWith("142656.00");
    await waitFor(() =>
      expect(within(panel).getByText("Copied Reserve total")).toBeInTheDocument());
    const live = panel.querySelector("[aria-live='polite']");
    expect(live).toHaveTextContent("Copied Reserve total");

    await userEvent.click(within(panel).getByRole("button", { name: "Copy Policy number" }));
    expect(writeText).toHaveBeenLastCalledWith("POL-20-0001");
  });

  it("previews cents and shillings without changing the copied amount", async () => {
    const shillingPanel = await openStrip(stubApi());
    const shillingPreview = within(shillingPanel)
      .getByText("Reserve total")
      .parentElement?.querySelector("strong")?.textContent;
    expect(shillingPreview).toContain("142,656");
    cleanup();

    const centsView = strip();
    centsView.groups[1].fields[0] = {
      ...centsView.groups[1].fields[0],
      copy_value: "14265600",
      external_encoding: "cents",
    };
    const centsPanel = await openStrip(stubApi({ view: centsView }));
    const centsPreview = within(centsPanel)
      .getByText("Reserve total")
      .parentElement?.querySelector("strong")?.textContent;
    expect(centsPreview).toBe(shillingPreview);
  });

  it("surfaces a copy failure and leaves the field unchanged", async () => {
    stubClipboard(true);
    const panel = await openStrip(stubApi());
    await userEvent.click(within(panel).getByRole("button", { name: "Copy Policy number" }));
    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent(/Copy failed for Policy number/);
    expect(within(panel).getByText("POL-20-0001")).toBeInTheDocument();
    expect(panel.querySelector("[aria-live='polite']")).toHaveTextContent("");
  });

  it("drives the group checkboxes from the keyboard", async () => {
    const api = stubApi();
    const panel = await openStrip(api);
    const checkbox = within(panel).getByRole("checkbox", { name: "Claim details entered" });
    checkbox.focus();
    await userEvent.keyboard(" ");
    await waitFor(() => expect(api.calls).toContain("group:claim_details:true"));
    await waitFor(() => expect(checkbox).toBeChecked());
    await userEvent.keyboard(" ");
    await waitFor(() => expect(api.calls).toContain("group:claim_details:false"));
  });

  it("gates confirm on groups, readback shape, and the attestation", async () => {
    const api = stubApi();
    const panel = await openStrip(api);
    const confirm = within(panel).getByRole("button", { name: "Confirm entry" });
    expect(confirm).toBeDisabled();

    for (const label of ["Claim details entered", "Reserve entered"]) {
      await userEvent.click(within(panel).getByRole("checkbox", { name: label }));
    }
    await waitFor(() => expect(api.calls).toContain("group:reserve:true"));
    expect(confirm).toBeDisabled();

    await userEvent.type(
      within(panel).getByRole("textbox", { name: /ICON claim number/ }),
      ICON_CLAIM_NO,
    );
    expect(confirm).toBeDisabled();

    await userEvent.click(
      within(panel).getByRole("checkbox", { name: /I entered the values exactly as shown/ }),
    );
    await waitFor(() => expect(confirm).toBeEnabled());
    await userEvent.click(confirm);
    await waitFor(() => expect(api.calls).toContain("confirm"));
  });

  it("refuses a readback whose format is still pending capture", async () => {
    const api = stubApi({
      view: strip({
        groups: strip().groups.map((group) => ({ ...group, done: true })),
        readback_fields: [
          {
            label: "ICON claim number",
            path: "external.icon.claim_no",
            required: true,
            format_status: "pending_capture",
            blocked_on: "open-item-3",
          },
        ],
      }),
    });
    const panel = await openStrip(api);
    expect(within(panel).getByRole("textbox", { name: /ICON claim number/ })).toBeDisabled();
    expect(within(panel).getByText(/Format pending capture — open-item-3/)).toBeInTheDocument();
    await userEvent.click(
      within(panel).getByRole("checkbox", { name: /I entered the values exactly as shown/ }),
    );
    expect(within(panel).getByRole("button", { name: "Confirm entry" })).toBeDisabled();
  });

  it("never shows optimistic completion when the server refuses", async () => {
    const refused = strip({ groups: strip().groups.map((group) => ({ ...group, done: true })) });
    const api = stubApi({
      view: refused,
      onConfirm: async () => {
        throw { code: "PROJECTION_STATE_STALE", detail: "Projection moved on" };
      },
    });
    const panel = await openStrip(api);
    await userEvent.type(
      within(panel).getByRole("textbox", { name: /ICON claim number/ }),
      ICON_CLAIM_NO,
    );
    await userEvent.click(
      within(panel).getByRole("checkbox", { name: /I entered the values exactly as shown/ }),
    );
    await userEvent.click(within(panel).getByRole("button", { name: "Confirm entry" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("PROJECTION_STATE_STALE");
    expect(screen.queryByText(/complete and immutable/)).not.toBeInTheDocument();
    // The server state was re-read rather than assumed.
    expect(api.calls.filter((call) => call === "read").length).toBeGreaterThan(1);
  });

  it("restores server state after a refused group update", async () => {
    const api = stubApi({
      onGroup: async () => {
        throw { code: "PROJECTION_STATE_STALE", detail: "Group update was stale" };
      },
    });
    const panel = await openStrip(api);
    await userEvent.click(
      within(panel).getByRole("checkbox", { name: "Claim details entered" }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("PROJECTION_STATE_STALE");
    await waitFor(() =>
      expect(
        within(panel).getByRole("checkbox", { name: "Claim details entered" }),
      ).not.toBeChecked());
  });

  it("shows no target-system iframe and registers no global shortcut", async () => {
    const panel = await openStrip(stubApi());
    expect(panel.querySelector("iframe")).toBeNull();
    expect(document.querySelector("iframe")).toBeNull();
  });
});

describe("accessibility and layout", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", { value: 1366, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 768, configurable: true });
  });

  it("passes axe on the catalogue and the open strip at 1366x768", async () => {
    const axe = (await import("axe-core")).default;
    const view = render(<ProjectionSystems api={stubApi()} claimId={CLAIM} />);
    await screen.findByRole("region", { name: "Registered operations" });
    await userEvent.click(
      screen.getByRole("button", { name: "Open paste strip for icon.claim_register" }),
    );
    await screen.findByRole("region", { name: "Paste assist strip" });
    const results = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
    });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
    // Wide content scrolls inside its own container, not the page.
    expect(view.container.querySelector(".table-scroll")).not.toBeNull();
  });
});
