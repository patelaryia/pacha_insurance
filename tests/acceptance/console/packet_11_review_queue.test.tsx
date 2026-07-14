/**
 * PACKET-11 acceptance (vitest) — PRD-04 §4.3 S-1 keyboard focus rules.
 *
 * Protected (CODEOWNERS): the builder may not modify this file. Runs inside
 * the console vitest config via its include glob and the @console alias
 * (docs/packets/PACKET-11_console_shell.md §3.1). Verbatim rules under test:
 * roving focus on the list/item container; a/e/r require an explicitly
 * focused item; all keys disabled when focus is in any
 * input/textarea/contenteditable; Esc returns focus to the list;
 * no global keydown handlers.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";

import { ReviewQueue } from "@console/screens/ReviewQueue";

const ITEMS = [
  {
    id: "01HITEMNOTE000000000000AAA",
    claim_id: "01HCLAIM000000000000000AAA",
    type: "NOTE_REVIEW",
    subtype: null,
    status: "open",
    assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
    payload: {
      capability_id: "pack.note_draft",
      output: { note: "draft" },
      citations: [{ document_id: "d", page: 1 }],
    },
    sla: [{ definition_id: "ack", state: "running" }],
  },
  {
    id: "01HITEMFIELD00000000000BBB",
    claim_id: "01HCLAIM000000000000000AAA",
    type: "FIELD_VERIFY",
    subtype: null,
    status: "open",
    assigned_to: "user:01HCONSOLEOFFICER00000AAAA",
    payload: {
      capability_id: "docintel.extract",
      output: { path: "vehicle.reg", value: "KDA 123A" },
      citations: [{ document_id: "d", page: 1 }],
    },
    sla: [],
  },
];

function makeApi(overrides: Record<string, unknown> = {}) {
  return {
    listReviews: vi.fn(async () => ({ items: ITEMS })),
    getReview: vi.fn(async (id: string) => ITEMS.find((i) => i.id === id)),
    resolveReview: vi.fn(async () => ({ status: "resolved" })),
    getClaim: vi.fn(async () => ({})),
    getTimeline: vi.fn(async () => ({ events: [] })),
    listDocuments: vi.fn(async () => ({ documents: [] })),
    getFinancials: vi.fn(async () => ({ runs: [] })),
    getFsmStates: vi.fn(async () => ({
      primary_path: [],
      write_off_path: [],
      terminal: [],
    })),
    getDocumentBlobUrl: vi.fn((id: string) => `/api/console/documents/${id}/blob`),
    ...overrides,
  };
}

async function renderQueue(api = makeApi()) {
  render(<ReviewQueue api={api as never} />);
  await waitFor(() =>
    expect(screen.getByTestId(`review-item-${ITEMS[0].id}`)).toBeTruthy(),
  );
  return api;
}

describe("S-1 keyboard focus rules (PRD-04 §4.3 verbatim)", () => {
  it("j/k move roving focus across items", async () => {
    await renderQueue();
    const list = screen.getByTestId("review-list");
    list.focus();
    fireEvent.keyDown(document.activeElement as Element, { key: "j" });
    expect(document.activeElement).toBe(
      screen.getByTestId(`review-item-${ITEMS[0].id}`),
    );
    fireEvent.keyDown(document.activeElement as Element, { key: "j" });
    expect(document.activeElement).toBe(
      screen.getByTestId(`review-item-${ITEMS[1].id}`),
    );
    fireEvent.keyDown(document.activeElement as Element, { key: "k" });
    expect(document.activeElement).toBe(
      screen.getByTestId(`review-item-${ITEMS[0].id}`),
    );
  });

  it("a approves the explicitly focused item with the PACKET-10 body", async () => {
    const api = await renderQueue();
    const first = screen.getByTestId(`review-item-${ITEMS[0].id}`);
    first.focus();
    fireEvent.keyDown(first, { key: "a" });
    await waitFor(() => expect(api.resolveReview).toHaveBeenCalledTimes(1));
    const [calledId, body] = api.resolveReview.mock.calls[0];
    expect(calledId).toBe(ITEMS[0].id);
    expect(body.action).toBe("approve");
    expect(body.schema_version).toBe("NOTE_REVIEW@1");
    expect(body).not.toHaveProperty("resolution");
  });

  it("a/e/r do nothing without an explicitly focused item", async () => {
    const api = await renderQueue();
    const list = screen.getByTestId("review-list");
    list.focus();
    for (const key of ["a", "e", "r"]) {
      fireEvent.keyDown(list, { key });
    }
    expect(api.resolveReview).not.toHaveBeenCalled();
  });

  it("reject requires a reason and shortcuts are dead inside the textarea", async () => {
    const api = await renderQueue();
    const first = screen.getByTestId(`review-item-${ITEMS[0].id}`);
    first.focus();
    fireEvent.keyDown(first, { key: "r" });
    const reason = (await screen.findByTestId("reject-reason")) as HTMLTextAreaElement;
    const submit = screen.getByTestId("reject-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    reason.focus();
    for (const key of ["a", "e", "r", "j", "k"]) {
      fireEvent.keyDown(reason, { key });
    }
    expect(api.resolveReview).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(reason);

    fireEvent.change(reason, { target: { value: "wrong figure cited" } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(api.resolveReview).toHaveBeenCalledTimes(1));
    const [, body] = api.resolveReview.mock.calls[0];
    expect(body.action).toBe("reject");
    expect(body.payload.reason).toBe("wrong figure cited");
  });

  it("Esc returns focus to the list", async () => {
    await renderQueue();
    const first = screen.getByTestId(`review-item-${ITEMS[0].id}`);
    first.focus();
    fireEvent.keyDown(first, { key: "Escape" });
    expect(document.activeElement).toBe(screen.getByTestId("review-list"));
  });
});
