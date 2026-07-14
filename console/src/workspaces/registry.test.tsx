import "@testing-library/jest-dom/vitest";

import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ConsoleApi, ReviewItem } from "../api/types";
import { Workspace } from "./registry";

afterEach(cleanup);

function api(): ConsoleApi {
  return {
    listReviews: vi.fn(),
    resolveReview: vi.fn().mockResolvedValue(undefined),
    getClaim360: vi.fn(),
    getCitation: vi.fn(),
  };
}

function item(payload: Record<string, unknown>): ReviewItem {
  return {
    id: "review-1", claim_id: "claim-1", type: "FIELD_VERIFY", subtype: null,
    status: "open", assigned_to: null, workspace_layout: "field_verify",
    resolution_schema: "FIELD_VERIFY@1", sla: [],
    payload: { path: "reserve.total", capability_id: "reserve.calculate", candidate_value: "123", ...payload },
  };
}

describe("typed FIELD_VERIFY", () => {
  it("blocks every action when capability attribution is missing", () => {
    const service = api();
    render(<Workspace item={item({ capability_id: undefined, value_type: "string" })} api={service} />);
    expect(screen.getByRole("alert")).toHaveTextContent("must be repaired by its producer");
    screen.getAllByRole("button").forEach((button) => expect(button).toBeDisabled());
  });

  it.each([
    ["money", "900719925474099312345", 900719925474099312345n, "money", {}],
    ["bool", "false", false, "enum", {}],
    ["date", "2026-07-14", "2026-07-14", "date", {}],
    ["enum", "approved", "approved", "enum", { allowed_values: ["pending", "approved"] }],
  ] as const)("submits a %s correction with its real type", async (valueType, entry, expected, kind, extras) => {
    const service = api();
    render(<Workspace item={item({ value_type: valueType, ...extras })} api={service} />);
    const input = screen.getByLabelText("Corrected value");
    if (input instanceof HTMLSelectElement) {
      await userEvent.selectOptions(input, entry);
    } else {
      await userEvent.clear(input);
      await userEvent.type(input, entry);
    }
    await userEvent.click(screen.getByRole("button", { name: "Edit→Approve" }));
    expect(service.resolveReview).toHaveBeenCalledWith("review-1", expect.objectContaining({
      payload: expect.objectContaining({
        corrected_fields: { "reserve.total": expected },
        diff: { typed_changes: [{ path: "reserve.total", kind }], prose_change_ratio: 0 },
      }),
    }));
  });

  it("parses object corrections only with explicit diff metadata", async () => {
    const service = api();
    const { rerender } = render(<Workspace item={item({ value_type: "object", candidate_value: {} })} api={service} />);
    expect(screen.getByRole("button", { name: "Edit→Approve" })).toBeDisabled();

    rerender(<Workspace item={item({ value_type: "object", candidate_value: {}, diff_kind: "party" })} api={service} />);
    const editor = screen.getByLabelText("Corrected value");
    fireEvent.change(editor, { target: { value: '{"name":"Amina"}' } });
    await userEvent.click(screen.getByRole("button", { name: "Edit→Approve" }));
    expect(service.resolveReview).toHaveBeenCalledWith("review-1", expect.objectContaining({
      payload: expect.objectContaining({
        corrected_fields: { "reserve.total": { name: "Amina" } },
        diff: { typed_changes: [{ path: "reserve.total", kind: "party" }], prose_change_ratio: 0 },
      }),
    }));
  });

  it("converts an Africa/Nairobi datetime correction to UTC", async () => {
    const service = api();
    render(<Workspace item={item({
      path: "intimation.received_at",
      value_type: "datetime",
      candidate_value: "2026-07-14T08:00:00Z",
    })} api={service} />);
    const editor = screen.getByLabelText("Corrected value");
    expect(editor).toHaveValue("2026-07-14T11:00");
    fireEvent.change(editor, { target: { value: "2026-07-14T12:30" } });
    await userEvent.click(screen.getByRole("button", { name: "Edit→Approve" }));
    expect(service.resolveReview).toHaveBeenCalledWith("review-1", expect.objectContaining({
      payload: expect.objectContaining({
        corrected_fields: { "intimation.received_at": "2026-07-14T09:30:00Z" },
      }),
    }));
  });

  it("keeps invalid typed values local and rejects only with a reason", async () => {
    const service = api();
    render(<Workspace item={item({ value_type: "date" })} api={service} />);
    await userEvent.type(screen.getByLabelText("Corrected value"), "invalid");
    await userEvent.click(screen.getByRole("button", { name: "Edit→Approve" }));
    expect(await screen.findByText("PAYLOAD_INVALID")).toBeVisible();
    expect(service.resolveReview).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByText("REASON_REQUIRED")).toBeVisible();
    const reason = screen.getByRole("textbox", { name: /rejection reason/i });
    fireEvent.keyDown(reason, { key: "Escape" });
    expect(screen.queryByRole("textbox", { name: /rejection reason/i })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await userEvent.type(screen.getByRole("textbox", { name: /rejection reason/i }), "Evidence mismatch");
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(service.resolveReview).toHaveBeenCalled();
  });
});
