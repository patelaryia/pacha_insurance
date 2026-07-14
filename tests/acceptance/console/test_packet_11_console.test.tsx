/** PACKET-11 protected browser acceptance. Builder must not modify this file. */
import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleApiClient } from "../../../console/src/api/client";
import type { Claim360, ConsoleApi, ReviewItem } from "../../../console/src/api/types";
import { CitationOverlay } from "../../../console/src/components/CitationOverlay";
import { Claim360Page } from "../../../console/src/pages/Claim360Page";
import { ReviewQueuePage } from "../../../console/src/pages/ReviewQueuePage";
import {
  WORKSPACE_COMPONENTS,
  Workspace,
} from "../../../console/src/workspaces/registry";
import { formatKes } from "../../../console/src/lib/money";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const TYPES = [
  "FIELD_VERIFY",
  "DOC_CLASSIFY",
  "DOC_SPLIT",
  "CONSISTENCY_FLAG",
  "DRAFT_RELEASE",
  "MODE_CONFIRM",
  "NOTE_REVIEW",
  "PACK_REVIEW",
  "EX_GRATIA",
  "EXCEPTION",
  "PROMOTION_SIGNOFF",
  "SAMPLE_REVIEW",
  "PASTE_READBACK_CHECK",
  "PROCEED_PARTIAL",
  "KYC_VERIFY",
  "EFT_MATCH",
  "REOPEN_PROMPT",
] as const;

const LAYOUTS = [
  "field_verify",
  "document_classification",
  "document_split",
  "consistency_evidence",
  "draft_release",
  "mode_confirmation",
  "note_review",
  "pack_review",
  "ex_gratia_review",
  "exception_detail",
  "promotion_signoff",
  "sampled_output",
  "paste_readback",
  "partial_documents",
  "kyc_verification",
  "eft_match",
  "reopen_prompt",
] as const;

function review(
  id: string,
  type: (typeof TYPES)[number] = "FIELD_VERIFY",
  layout = "field_verify",
): ReviewItem {
  return {
    id,
    claim_id: `claim-${id}`,
    type,
    subtype: null,
    status: "open",
    assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
    payload: {
      path: "vehicle.reg",
      candidate_value: "KAA 111B",
      capability_id: "doc.extract.registration",
    },
    workspace_layout: layout,
    resolution_schema: `${type}@1`,
    sla: [{ definition_id: "sla.acknowledge", state: "running" }],
  };
}

function claim360(status = "RESERVED"): Claim360 {
  return {
    claim: {
      id: "claim-1",
      status,
      substatus: null,
      assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
      created_at: "2026-07-14T07:00:00Z",
      updated_at: "2026-07-14T08:00:00Z",
    },
    header: {
      insured: "Amina Wanjiku",
      registration: "KAA 111B",
      amount_cents: "123456",
    },
    fields: [
      {
        path: "vehicle.reg",
        value: "KAA 111B",
        value_type: "string",
        verification_state: "human_verified",
        confidence: null,
        source_type: "human",
        has_citation: true,
      },
    ],
    documents: [],
    financials: [
      { path: "reserve.total", amount_cents: "123456", calc_run_id: null },
    ],
    timeline: [],
    systems: [],
    communications: [],
    availability: {
      document_checklist: { status: "not_available", owner: "PRD-06" },
      systems: { status: "available", owner: "PRD-09" },
      communications: { status: "available", owner: "PRD-06" },
    },
  };
}

function api(overrides: Partial<ConsoleApi> = {}): ConsoleApi {
  return {
    listReviews: vi.fn().mockResolvedValue([review("one"), review("two", "NOTE_REVIEW", "note_review")]),
    resolveReview: vi.fn().mockResolvedValue(undefined),
    getClaim360: vi.fn().mockResolvedValue(claim360()),
    getCitation: vi.fn().mockResolvedValue({
      claim_id: "claim-1",
      field_path: "vehicle.reg",
      value: "KAA 111B",
      value_type: "string",
      verification_state: "human_verified",
      document_id: "document-1",
      page: 1,
      bbox: [0.1, 0.2, 0.4, 0.3],
      document_url: "/console/documents/document-1/normalised.pdf",
    }),
    ...overrides,
  };
}

describe("trusted browser API", () => {
  it("attaches only the bearer token and never X-Actor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ConsoleApiClient({
      baseUrl: "https://api.example.test",
      getAccessToken: async () => "signed-access-token",
    });

    await client.listReviews({ scope: "mine" });

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer signed-access-token");
    expect(headers.has("X-Actor")).toBe(false);
  });
});

describe("S-1 review queue", () => {
  it("supports mine/pool and type/status filters", async () => {
    const service = api();
    render(<ReviewQueuePage api={service} />);
    await screen.findAllByRole("option");

    await userEvent.click(screen.getByRole("button", { name: "Pool" }));
    await userEvent.selectOptions(screen.getByLabelText("Item type"), "NOTE_REVIEW");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "open");

    await waitFor(() => {
      expect(service.listReviews).toHaveBeenLastCalledWith({
        scope: "pool",
        type: "NOTE_REVIEW",
        status: "open",
      });
    });
  });

  it("uses container-scoped roving focus and suppresses shortcuts in inputs", async () => {
    const service = api();
    render(<ReviewQueuePage api={service} />);
    const options = await screen.findAllByRole("option");
    const list = screen.getByRole("listbox", { name: "Review items" });

    fireEvent.keyDown(list, { key: "a" });
    expect(service.resolveReview).not.toHaveBeenCalled();

    options[0].focus();
    fireEvent.keyDown(options[0], { key: "j" });
    expect(options[1]).toHaveFocus();
    expect(options[0]).toHaveAttribute("tabindex", "-1");
    expect(options[1]).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(options[1], { key: "k" });
    expect(options[0]).toHaveFocus();

    const input = screen.getByRole("textbox", { name: "Corrected value" });
    input.focus();
    fireEvent.keyDown(input, { key: "r" });
    expect(service.resolveReview).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(options[0]).toHaveFocus();

    fireEvent.keyDown(options[0], { key: "a", ctrlKey: true });
    expect(service.resolveReview).not.toHaveBeenCalled();
  });

  it("shows exactly three primary actions and keeps a failed item visible", async () => {
    const service = api({
      resolveReview: vi.fn().mockRejectedValue({
        code: "RESOLUTION_BLOCKED_ON_INPUTS",
        detail: "Required field missing",
      }),
    });
    render(<ReviewQueuePage api={service} />);
    const [first] = await screen.findAllByRole("option");
    first.focus();

    const actions = screen.getByRole("group", { name: "Resolution actions" });
    expect(actions.querySelectorAll("button")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Approve" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Edit→Approve" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject" })).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText("RESOLUTION_BLOCKED_ON_INPUTS")).toBeVisible();
    expect(screen.getByRole("option", { name: /FIELD_VERIFY/ })).toBeVisible();
  });

  it("binds all 17 layouts explicitly and fails closed on an unknown layout", () => {
    expect(Object.keys(WORKSPACE_COMPONENTS).sort()).toEqual([...LAYOUTS].sort());

    const service = api();
    const { rerender } = render(
      <Workspace item={review("known")} api={service} />,
    );
    expect(screen.queryByText("Unsupported workspace")).not.toBeInTheDocument();

    rerender(
      <Workspace
        item={review("unknown", "FIELD_VERIFY", "invented_layout")}
        api={service}
    />,
    );
    expect(screen.getByText("Unsupported workspace")).toBeVisible();
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });
});

describe("S-2 Claim 360", () => {
  it("renders all seven tabs, the 24-state rail and a structural decline banner", async () => {
    const service = api({ getClaim360: vi.fn().mockResolvedValue(claim360("DECLINED")) });
    render(<Claim360Page api={service} claimId="claim-1" />);

    expect(await screen.findByRole("alert", { name: "Claim declined" })).toBeVisible();
    expect(screen.getAllByRole("listitem", { name: /Claim state/ })).toHaveLength(24);
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Overview",
      "Documents",
      "Fields & Citations",
      "Financials",
      "Timeline",
      "Systems",
      "Communications",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Reopen claim" }));
    expect(screen.getByText("Reopen unavailable — PRD-05")).toBeVisible();
  });

  it("formats integer-cent money without a JavaScript number", () => {
    expect(formatKes(123_456n)).toBe("KES 1,234.56");
    expect(formatKes(123_400n)).toBe("KES 1,234");
    expect(formatKes(-5n)).toBe("KES -0.05");
  });
});

describe("citation overlay", () => {
  function expected(
    bbox: readonly [number, number, number, number],
    width: number,
    height: number,
    rotation: 0 | 90 | 180 | 270,
  ) {
    const [x0, y0, x1, y1] = bbox;
    if (rotation === 90) {
      return { left: (1 - y1) * width, top: x0 * height, width: (y1 - y0) * width, height: (x1 - x0) * height };
    }
    if (rotation === 180) {
      return { left: (1 - x1) * width, top: (1 - y1) * height, width: (x1 - x0) * width, height: (y1 - y0) * height };
    }
    if (rotation === 270) {
      return { left: y0 * width, top: (1 - x1) * height, width: (y1 - y0) * width, height: (x1 - x0) * height };
    }
    return { left: x0 * width, top: y0 * height, width: (x1 - x0) * width, height: (y1 - y0) * height };
  }

  it("places 50 stored normalised boxes exactly at scale and rotation", () => {
    const rotations = [0, 90, 180, 270] as const;
    const { rerender } = render(
      <CitationOverlay bbox={[0.01, 0.02, 0.11, 0.12]} viewport={{ width: 600, height: 800, rotation: 0 }} label="Cited value" />,
    );

    for (let index = 0; index < 50; index += 1) {
      const x0 = 0.01 + (index % 10) * 0.07;
      const y0 = 0.02 + Math.floor(index / 10) * 0.12;
      const bbox = [x0, y0, x0 + 0.05, y0 + 0.04] as const;
      const rotation = rotations[index % rotations.length];
      const viewport = { width: rotation % 180 === 0 ? 600 : 800, height: rotation % 180 === 0 ? 800 : 600, rotation };
      rerender(<CitationOverlay bbox={bbox} viewport={viewport} label={`Citation ${index + 1}`} />);
      const overlay = screen.getByLabelText(`Citation ${index + 1}`);
      const result = expected(bbox, viewport.width, viewport.height, rotation);
      expect(overlay).toHaveStyle({
        left: `${result.left}px`,
        top: `${result.top}px`,
        width: `${result.width}px`,
        height: `${result.height}px`,
      });
    }
  });
});
