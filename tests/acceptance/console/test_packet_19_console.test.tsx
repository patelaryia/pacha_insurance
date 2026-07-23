/** PACKET-19 protected browser acceptance. Builder must not weaken this file.
 *
 * PRD-08 §8.5 / PRD-04 §4.3 S-2/S-3 per docs/packets/PACKET-19_approval_workflow.md
 * §8/§10. No network, no live pdf.js worker, no real timers: the merged-pack and
 * signed-note panes assert the authenticated artifact URL the console requests,
 * and autosave is proved on fake timers.
 */
import "@testing-library/jest-dom/vitest";

import React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ApprovalNoteWorkspace as NoteWorkspace,
  ConsoleApi,
  PackReadiness,
  ReviewItem,
} from "../../../console/src/api/types";
import { ApprovalPackReadiness } from "../../../console/src/components/ApprovalPackReadiness";
import { ApprovalsPage } from "../../../console/src/pages/ApprovalsPage";
import { Workspace } from "../../../console/src/workspaces/registry";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const CLAIM = "01HP19CLAIM0000000000000AA";
const REVIEW = "01HP19NOTEREVIEW00000000AA";
const PACK_REVIEW = "01HP19PACKREVIEW00000000AA";
const DRAFT = "01HP19DRAFT0000000000000AA";
const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

const MANIFEST_IDS = [
  "policy_document", "intimation_email", "claim_form", "logbook", "driving_licence",
  "kra_pin_cert", "photos", "repair_estimate", "assessor_engagement_email",
  "assessor_report", "supplier_quotes", "assessor_payment_request",
  "claim_details_report",
];

function readiness(overrides: Partial<PackReadiness> = {}): PackReadiness {
  return {
    claim_id: CLAIM,
    status: "RESERVED",
    ready: false,
    fingerprint: "fingerprint-1",
    checklists: { ready: true, blockers: [] },
    fields: { ready: true, blockers: [] },
    items: MANIFEST_IDS.map((id, index) => ({
      id,
      order: index + 1,
      label: id.replaceAll("_", " "),
      state: id === "claim_form" ? "ambiguous" : id === "claim_details_report"
        ? "pending_integration"
        : "ready",
      required: true,
      waivable: false,
      sources: id === "claim_form" ? [] : [
        {
          kind: "document",
          id: `doc-${id}`,
          filename: `${id}.pdf`,
          received_at: "2026-07-22T08:00:00Z",
          sha256: HASH_A,
        },
      ],
      blockers: id === "claim_form"
        ? [{ code: "ambiguous_sources", item_id: id, detail: "doc-1,doc-2" }]
        : [],
    })),
    blockers: [{ code: "ambiguous_sources", item_id: "claim_form", detail: "doc-1,doc-2" }],
    ...overrides,
  };
}

function noteWorkspace(overrides: Partial<NoteWorkspace> = {}): NoteWorkspace {
  return {
    review_id: REVIEW,
    review_status: "open",
    claim_id: CLAIM,
    root_draft_id: DRAFT,
    current_draft: {
      id: DRAFT,
      version: 1,
      status: "in_review",
      body_sha256: HASH_A,
      edited_by: null,
      body: {
        sections: [
          {
            template_slot: "computed",
            locked: true,
            content: [
              {
                slot: "assessed_amount",
                label: "Assessed amount",
                state: "resolved",
                locked: true,
                display: "KES 136,276",
                citation_marker: "[1]",
                source_ref: {
                  field_id: "F1",
                  path: "assessment.agreed_quote",
                  version: 3,
                  provenance: { user_id: "user:x" },
                },
              },
              {
                slot: "amount_payable",
                label: "Amount payable",
                state: "pending_capture",
                locked: true,
                display: "PENDING CAPTURE",
                blocker: "register #5: C-08 payable formula uncaptured",
                source_ref: null,
              },
            ],
          },
          {
            template_slot: "verification",
            locked: true,
            content: [
              {
                slot: "narrative_photo_consistency",
                label: "Narrative and photograph consistency",
                state: "flagged",
                locked: true,
                display: "flagged",
                evidence: [{ id: "CC1", check_id: "CC-5", status: "flagged" }],
              },
            ],
          },
          { template_slot: "incident_summary", locked: false, content: "A collision." },
          { template_slot: "excess_vs_max", locked: false, content: "The excess applies." },
          { template_slot: "savings_narrative", locked: false, content: "A saving arose." },
        ],
        blockers: [
          {
            slot: "amount_payable",
            state: "pending_capture",
            detail: "register #5: C-08 payable formula uncaptured",
          },
        ],
      },
    },
    merged_pack: {
      event_id: "EVT-MERGED",
      version: 1,
      sha256: HASH_B,
      content_url: `/claims/${CLAIM}/approval-pack/artifacts/EVT-MERGED`,
    },
    signed_note: null,
    sign_state: "unsigned",
    autosave_seconds: 5,
    commentary_slots: ["incident_summary", "excess_vs_max", "savings_narrative"],
    editable_slots: ["incident_summary", "excess_vs_max", "savings_narrative"],
    incident_summary_max_words: 80,
    icon_note_entry: {
      id: "icon.note_entry",
      status: "pending_capture",
      blocked_on: "open-item-3",
      fields: [],
    },
    signable: false,
    blockers: [
      {
        slot: "amount_payable",
        state: "pending_capture",
        detail: "register #5: C-08 payable formula uncaptured",
      },
    ],
    ...overrides,
  };
}

function noteItem(): ReviewItem {
  return {
    id: REVIEW,
    claim_id: CLAIM,
    type: "NOTE_REVIEW",
    subtype: "approval_note",
    status: "open",
    assigned_to: null,
    payload: { capability_id: "pack.note_draft", note_draft_id: DRAFT },
    workspace_layout: "approval_note_review",
    resolution_schema: "NOTE_REVIEW@2",
    sla: [],
  };
}

function packItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: PACK_REVIEW,
    claim_id: CLAIM,
    type: "PACK_REVIEW",
    subtype: "approval_pack",
    status: "open",
    assigned_to: null,
    payload: {
      capability_id: "pack.route",
      draft_id: DRAFT,
      body_sha256: HASH_A,
      merged_event_id: "EVT-MERGED",
      note_signed_event_id: "EVT-SIGNED",
      routing_amount_cents: 4_000_000_01,
      required_role: "chairman",
      route_provenance: {
        source: "claim_field",
        path: "reserve.total",
        field_id: "F9",
        field_version: 2,
        blocked_calc_id: "C-08",
        blocked_calc_status: "blocked_on_inputs",
      },
      side_effects: [{ template_id: "T-03", template_version: "1.0.0", blob_key: "k" }],
    },
    workspace_layout: "approval_pack_review",
    resolution_schema: "PACK_REVIEW@2",
    sla: [],
    ...overrides,
  };
}

function api(overrides: Partial<ConsoleApi> = {}): ConsoleApi {
  return {
    listReviews: vi.fn(async () => [packItem()]),
    resolveReview: vi.fn(async () => undefined),
    getClaim360: vi.fn(),
    getCitation: vi.fn(),
    // Artifacts are fetched through the authenticated console client only; the
    // test asserts the requested URL and never runs a pdf.js worker.
    getDocument: vi.fn(async () => new ArrayBuffer(8)),
    getPackReadiness: vi.fn(async () => readiness()),
    selectPackSources: vi.fn(async () => ({ recorded: true })),
    uploadPackItem: vi.fn(async () => ({ upload_id: "U1" })),
    generatePack: vi.fn(async () => ({ status: "ready_for_note_review", note_status: "in_review" })),
    getApprovalNote: vi.fn(async () => noteWorkspace()),
    saveApprovalNote: vi.fn(async () => ({
      draft_id: "01HP19DRAFT0000000000000BB",
      version: 2,
      body_sha256: HASH_B,
      parent_draft_id: DRAFT,
      review_id: REVIEW,
      recorded: true,
    })),
    ...overrides,
  } as unknown as ConsoleApi;
}

// --- 1. Claim-360 readiness card ----------------------------------------------------

describe("S-2 approval-pack readiness card", () => {
  it("renders all 13 rows in manifest order with blockers and source selection", async () => {
    const client = api();
    render(
      <ApprovalPackReadiness
        api={client}
        claimId={CLAIM}
        documents={[{ id: "doc-1", filename: "claim-form.pdf" }]}
        communications={[{ id: "comm-1", subject: "Intimation email" }]}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("readiness-row-policy_document")).toBeInTheDocument());
    const rows = MANIFEST_IDS.map((id) => screen.getByTestId(`readiness-row-${id}`));
    expect(rows).toHaveLength(13);
    for (let index = 1; index < rows.length; index += 1) {
      expect(
        rows[index - 1].compareDocumentPosition(rows[index])
          & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    }
    const ambiguous = screen.getByTestId("readiness-row-claim_form");
    expect(ambiguous.textContent).toContain("Ambiguous");
    expect(ambiguous.textContent).toContain("ambiguous_sources");
    expect(screen.getByTestId("readiness-row-claim_details_report").textContent)
      .toContain("Pending integration");

    // Only same-claim sources Claim 360 already returned are offered.
    const select = within(ambiguous).getByLabelText("Select claim form");
    expect(within(select as HTMLElement).getAllByRole("option")).toHaveLength(3);
    fireEvent.change(select, { target: { value: "document:doc-1" } });
    await waitFor(() =>
      expect(client.selectPackSources).toHaveBeenCalledWith(CLAIM, "claim_form", [
        { kind: "document", id: "doc-1" },
      ]));

    // Generation is refused while the card is not ready.
    expect(screen.getByTestId("generate-pack")).toBeDisabled();
  });

  it("uploads items 12-13, shows an accessible error, and never claims a stale success", async () => {
    const failing = vi.fn(async () => {
      throw { code: "INVALID_PDF", detail: "Upload must be a PDF" };
    });
    const ready = readiness({ ready: true, blockers: [] });
    const client = api({
      uploadPackItem: failing,
      getPackReadiness: vi.fn(async () => ready),
      generatePack: vi.fn(async () => {
        throw { code: "READINESS_STALE", detail: "The card changed" };
      }),
    });
    render(
      <ApprovalPackReadiness api={client} claimId={CLAIM} documents={[]} communications={[]} />,
    );
    await waitFor(() => expect(screen.getByTestId("generate-pack")).toBeEnabled());

    const input = screen.getByLabelText("Upload assessor payment request");
    fireEvent.change(input, {
      target: { files: [new File(["x"], "a.txt", { type: "text/plain" })] },
    });
    const failure = await screen.findByTestId("upload-error-assessor_payment_request");
    expect(failure).toHaveAttribute("role", "alert");
    expect(failure.textContent).toContain("INVALID_PDF");

    fireEvent.click(screen.getByTestId("generate-pack"));
    const error = await screen.findByTestId("generation-error");
    expect(error.textContent).toContain("READINESS_STALE");
    expect(screen.queryByTestId("generation-outcome")).not.toBeInTheDocument();
    // The card is refreshed rather than left showing a stale fingerprint.
    expect(client.getPackReadiness).toHaveBeenCalledTimes(2);
  });

  it("reports the exact server outcome for a staged generation", async () => {
    const client = api({
      getPackReadiness: vi.fn(async () => readiness({ ready: true, blockers: [] })),
      generatePack: vi.fn(async () => ({
        status: "staged",
        capability_id: "pack.merge",
        review_item_id: "R1",
      })),
    });
    render(
      <ApprovalPackReadiness api={client} claimId={CLAIM} documents={[]} communications={[]} />,
    );
    await waitFor(() => expect(screen.getByTestId("generate-pack")).toBeEnabled());
    fireEvent.click(screen.getByTestId("generate-pack"));
    const outcome = await screen.findByTestId("generation-outcome");
    expect(outcome.textContent).toContain("Staged for release");
    expect(outcome.textContent).toContain("pack.merge");
  });
});

// --- 2/3. NOTE_REVIEW split workspace and autosave ----------------------------------

describe("NOTE_REVIEW approval-note workspace", () => {
  it("locks evidence, keeps only commentary editable, and shows blockers beside Sign", async () => {
    const client = api();
    render(<Workspace item={noteItem()} api={client} />);
    await screen.findByTestId("note-slot-assessed_amount");

    const locked = screen.getByTestId("note-slot-assessed_amount");
    expect(locked.textContent).toContain("KES 136,276");
    expect(within(locked).queryByRole("textbox")).toBeNull();
    expect(screen.getByTestId("note-citation-assessed_amount").textContent)
      .toContain("assessment.agreed_quote");
    // A CC-5 flag is copied verbatim and never laundered into a pass.
    expect(screen.getByTestId("note-slot-narrative_photo_consistency").textContent)
      .toContain("flagged");
    // The uncaptured payable slot renders its placeholder with no value.
    expect(screen.getByTestId("note-slot-amount_payable").textContent)
      .toContain("PENDING CAPTURE");

    for (const slot of ["incident_summary", "excess_vs_max", "savings_narrative"]) {
      expect(screen.getByTestId(`commentary-${slot}`)).toBeEnabled();
    }
    const blockers = screen.getByTestId("sign-blockers");
    expect(blockers.textContent).toContain("C-08");
    expect(screen.queryByRole("button", { name: /dismiss blocker/i })).toBeNull();
    expect(screen.getByRole("button", { name: "Sign" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save & Sign" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();

    // The merged pack is fetched only through the authenticated artifact route.
    await waitFor(() =>
      expect(client.getDocument).toHaveBeenCalledWith(
        `/claims/${CLAIM}/approval-pack/artifacts/EVT-MERGED`,
      ));
    expect(screen.getByTestId("icon-note-entry").textContent).toContain("pending_capture");
    expect(screen.getByTestId("icon-note-entry").textContent).toContain("open-item-3");
  });

  it("autosaves five seconds after a keystroke and on blur, never sooner", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = api();
    render(<Workspace item={noteItem()} api={client} />);
    const field = await screen.findByTestId("commentary-incident_summary");

    fireEvent.change(field, { target: { value: "A revised incident summary." } });
    await act(async () => {
      vi.advanceTimersByTime(4_000);
    });
    expect(client.saveApprovalNote).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(1_100);
    });
    await waitFor(() => expect(client.saveApprovalNote).toHaveBeenCalledTimes(1));
    const [reviewId, body] = (client.saveApprovalNote as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(reviewId).toBe(REVIEW);
    expect(body.base_draft_id).toBe(DRAFT);
    expect(body.base_body_sha256).toBe(HASH_A);
    expect(body.commentary).toHaveLength(3);
    expect(body.commentary[0]).toEqual({
      template_slot: "incident_summary",
      content: "A revised incident summary.",
    });
    await waitFor(() =>
      expect(screen.getByTestId("autosave-state").textContent).toContain("Saved at"));

    fireEvent.change(field, { target: { value: "Another revision." } });
    fireEvent.blur(field);
    await waitFor(() => expect(client.saveApprovalNote).toHaveBeenCalledTimes(2));
  });

  it("recovers local text on a stale-tab 409 and never overwrites the server version", async () => {
    let workspaceVersion = noteWorkspace();
    const client = api({
      getApprovalNote: vi.fn(async () => workspaceVersion),
      saveApprovalNote: vi.fn(async () => {
        workspaceVersion = noteWorkspace({
          current_draft: {
            ...noteWorkspace().current_draft,
            id: "01HP19DRAFT0000000000000CC",
            version: 4,
            body_sha256: HASH_B,
          },
        });
        throw { code: "STALE_NOTE_DRAFT", detail: "Another tab saved a newer version" };
      }),
    });
    render(<Workspace item={noteItem()} api={client} />);
    const field = await screen.findByTestId("commentary-incident_summary");
    fireEvent.change(field, { target: { value: "Local unsaved text." } });
    fireEvent.blur(field);

    const panel = await screen.findByTestId("recovery-panel");
    expect(panel).toHaveAttribute("role", "alert");
    expect(screen.getByTestId("recovered-incident_summary").textContent)
      .toBe("Local unsaved text.");
    expect(screen.getByTestId("autosave-state").textContent).toContain("Save failed");
    // The reload opened the highest server version.
    await waitFor(() =>
      expect(screen.getByTestId("commentary-incident_summary")).toHaveValue("A collision."));
  });

  it("surfaces a save failure and fires no keyboard shortcut while typing", async () => {
    const client = api({
      saveApprovalNote: vi.fn(async () => {
        throw { code: "COMMENTARY_INVALID", detail: "number 999999 is not supported" };
      }),
    });
    render(<Workspace item={noteItem()} api={client} />);
    const field = await screen.findByTestId("commentary-incident_summary");
    fireEvent.change(field, { target: { value: "KES 999,999 was paid." } });
    fireEvent.blur(field);
    await waitFor(() =>
      expect(screen.getByTestId("autosave-state").textContent)
        .toContain("COMMENTARY_INVALID"));

    // `a`, `e` and `r` inside a textarea must never resolve the item.
    for (const key of ["a", "e", "r"]) {
      fireEvent.keyDown(field, { key });
    }
    expect(client.resolveReview).not.toHaveBeenCalled();
  });

  it("Save & Sign autosaves first and signs the exact saved id and hash", async () => {
    let current = noteWorkspace({ signable: true, blockers: [] });
    const client = api({
      getApprovalNote: vi.fn(async () => current),
      saveApprovalNote: vi.fn(async () => {
        current = noteWorkspace({
          signable: true,
          blockers: [],
          current_draft: {
            ...noteWorkspace().current_draft,
            id: "01HP19DRAFT0000000000000DD",
            version: 2,
            body_sha256: HASH_B,
          },
        });
        return {
          draft_id: "01HP19DRAFT0000000000000DD",
          version: 2,
          body_sha256: HASH_B,
          parent_draft_id: DRAFT,
          review_id: REVIEW,
          recorded: true,
        };
      }),
    });
    render(<Workspace item={noteItem()} api={client} />);
    const field = await screen.findByTestId("commentary-incident_summary");
    fireEvent.change(field, { target: { value: "An edited summary." } });
    fireEvent.click(screen.getByRole("button", { name: "Save & Sign" }));

    await waitFor(() => expect(client.resolveReview).toHaveBeenCalled());
    expect(client.saveApprovalNote).toHaveBeenCalled();
    const [, request] = (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(request.action).toBe("approve");
    expect(request.schema_version).toBe("NOTE_REVIEW@2");
    expect(request.payload.draft_id).toBe("01HP19DRAFT0000000000000DD");
    expect(request.payload.body_sha256).toBe(HASH_B);
    // Prose never travels through the resolution endpoint.
    expect(JSON.stringify(request.payload)).not.toContain("An edited summary.");
  });

  it("requires a reason to reject and reports a server refusal", async () => {
    const client = api({
      signable: true,
      resolveReview: vi.fn(async () => {
        throw { code: "SIGN_BLOCKED_ON_INPUTS", detail: "C-08 is uncaptured" };
      }),
    } as Partial<ConsoleApi>);
    render(<Workspace item={noteItem()} api={client} />);
    await screen.findByTestId("commentary-incident_summary");

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByLabelText("Rejection reason")).toBeInTheDocument();
    expect(client.resolveReview).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Rejection reason"), {
      target: { value: "The savings narrative is wrong." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalled());
    expect((await screen.findByTestId("note-resolution-error")).textContent)
      .toContain("SIGN_BLOCKED_ON_INPUTS");
  });
});

// --- 4/5. S-3 exact-band queue and the three approval actions -----------------------

describe("S-3 approval workspace", () => {
  it("lists only the actor's exact-role items and shows both artifacts side by side", async () => {
    const client = api();
    render(<ApprovalsPage api={client} />);
    await waitFor(() => expect(screen.getByTestId("approval-queue")).toBeInTheDocument());
    expect(client.listReviews).toHaveBeenCalledWith(
      expect.objectContaining({ scope: "band", status: "open" }),
    );
    // The PRD-08 panes replace the unavailable placeholder for a routed pack.
    await waitFor(() =>
      expect(screen.queryByTestId("approval-pack-unavailable")).not.toBeInTheDocument());
    expect(await screen.findByTestId("merged-pack-pane")).toBeInTheDocument();
    expect(screen.getByTestId("signed-note-pane")).toBeInTheDocument();
    await waitFor(() =>
      expect(client.getDocument).toHaveBeenCalledWith(
        `/claims/${CLAIM}/approval-pack/artifacts/EVT-SIGNED`,
      ));
    // The routed figure is rendered from integer cents with its provenance.
    expect(screen.getByTestId("routing-amount").textContent).toContain("4,000,000.01");
    expect(screen.getByTestId("route-provenance").textContent).toContain("reserve.total");
    expect(screen.getByTestId("route-provenance").textContent).toContain("C-08");
    expect(screen.getByTestId("t03-state").textContent).toContain("T-03");
  });

  it("keeps the legacy PACK_REVIEW pane explicitly unavailable", async () => {
    const legacy = packItem({
      subtype: null,
      workspace_layout: "pack_review",
      resolution_schema: "PACK_REVIEW@1",
    });
    render(<ApprovalsPage api={api({ listReviews: vi.fn(async () => [legacy]) })} />);
    const unavailable = await screen.findByTestId("approval-pack-unavailable");
    expect(unavailable.textContent).toContain("no PRD-08");
  });

  it("maps Approve, Annotate & Approve, and Reject to the three closed actions", async () => {
    const client = api();
    render(<Workspace item={packItem()} api={client} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(1));
    let [, request] = (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(request.action).toBe("approve");
    expect(request.schema_version).toBe("PACK_REVIEW@2");
    expect(request.payload.required_role).toBe("chairman");
    expect(request.payload.routing_amount_cents).toBe(4_000_000_01);

    // Annotate & Approve refuses an empty annotation client-side too.
    fireEvent.click(screen.getByRole("button", { name: "Annotate & Approve" }));
    expect((await screen.findByTestId("approval-error")).textContent)
      .toContain("PAYLOAD_INVALID");
    expect(client.resolveReview).toHaveBeenCalledTimes(1);

    fireEvent.change(screen.getByTestId("manager-annotation"), {
      target: { value: "Confirm the garage on release." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Annotate & Approve" }));
    await waitFor(() => expect(client.resolveReview).toHaveBeenCalledTimes(2));
    [, request] = (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(request.action).toBe("edit_approve");
    expect(request.payload.annotation).toBe("Confirm the garage on release.");
  });

  it("requires structured reasons and pairs a named field path with the typed diff", async () => {
    const client = api();
    render(<Workspace item={packItem()} api={client} />);

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByTestId("rejection-reasons")).toBeInTheDocument();
    expect(client.resolveReview).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("Reason 1 code"), {
      target: { value: "figure_mismatch" },
    });
    fireEvent.change(screen.getByLabelText("Reason 1 detail"), {
      target: { value: "The agreed quote does not match the report." },
    });
    fireEvent.change(screen.getByLabelText("Reason 1 field path"), {
      target: { value: "assessment.agreed_quote" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));

    await waitFor(() => expect(client.resolveReview).toHaveBeenCalled());
    const [, request] = (client.resolveReview as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(request.action).toBe("reject");
    expect(request.payload.reasons).toEqual([
      {
        code: "figure_mismatch",
        detail: "The agreed quote does not match the report.",
        field_path: "assessment.agreed_quote",
      },
    ]);
    expect(request.payload.diff.typed_changes).toEqual([
      { path: "assessment.agreed_quote", kind: "text" },
    ]);
  });

  it("does not optimistically remove the item when the server refuses", async () => {
    const client = api({
      resolveReview: vi.fn(async () => {
        throw { code: "APPROVAL_ROUTE_STALE", detail: "The routing input changed" };
      }),
    });
    render(<ApprovalsPage api={client} />);
    await screen.findByTestId("merged-pack-pane");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect((await screen.findByTestId("approval-error")).textContent)
      .toContain("APPROVAL_ROUTE_STALE");
    expect(screen.getByTestId("merged-pack-pane")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-resolved")).not.toBeInTheDocument();
  });
});

// --- 6. accessibility and the 1366x768 desktop --------------------------------------

describe("accessibility and layout", () => {
  beforeEach(() => {
    vi.stubGlobal("innerWidth", 1366);
    vi.stubGlobal("innerHeight", 768);
  });

  it("passes axe on both approval workspaces at 1366x768", async () => {
    const axe = (await import("axe-core")).default;
    const noteHost = document.createElement("div");
    document.body.appendChild(noteHost);
    render(<Workspace item={noteItem()} api={api()} />, { container: noteHost });
    await screen.findByTestId("sign-blockers");
    const noteResult = await axe.run(noteHost, {
      rules: { region: { enabled: false } },
    });
    expect(noteResult.violations.map((violation) => violation.id)).toEqual([]);

    cleanup();
    const packHost = document.createElement("div");
    document.body.appendChild(packHost);
    render(<Workspace item={packItem()} api={api()} />, { container: packHost });
    await screen.findByTestId("merged-pack-pane");
    const packResult = await axe.run(packHost, {
      rules: { region: { enabled: false } },
    });
    expect(packResult.violations.map((violation) => violation.id)).toEqual([]);
  });

  it("keeps both approval panes reachable by keyboard only", async () => {
    render(<Workspace item={packItem()} api={api()} />);
    await screen.findByTestId("merged-pack-pane");
    const buttons = screen.getAllByRole("button");
    for (const button of buttons) {
      button.focus();
      expect(document.activeElement).toBe(button);
    }
  });
});
