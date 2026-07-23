import React, { useState } from "react";

import type { ConsoleApi, ResolutionAction, ReviewItem } from "../api/types";
import { formatStructured, parseLossless } from "../lib/json";
import { ApprovalNoteWorkspace } from "./ApprovalNoteWorkspace";
import { ApprovalPackWorkspace } from "./ApprovalPackWorkspace";
import { ProjectionWorkspace } from "./ProjectionWorkspace";

type FieldValueType = "string" | "money" | "date" | "datetime" | "bool" | "enum" | "object";
type ChangeKind = "money" | "date" | "party" | "enum" | "text";

const FIELD_VALUE_TYPES = new Set<FieldValueType>([
  "string", "money", "date", "datetime", "bool", "enum", "object",
]);
const CHANGE_KINDS = new Set<ChangeKind>(["money", "date", "party", "enum", "text"]);

interface LayoutProps {
  item: ReviewItem;
  correctedValue: string;
  editBlocked: string | null;
  onTextChange: (
    event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) => void;
}

function payloadValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "bigint") return value.toString();
  return formatStructured(value);
}

function initialEditorValue(item: ReviewItem, value: unknown): string {
  const raw = payloadValue(value);
  if (fieldType(item) !== "datetime" || typeof value !== "string") return raw;
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return raw;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return raw;
  const eatWallClock = new Date(parsed.getTime() + 3 * 60 * 60 * 1000);
  return eatWallClock.toISOString().slice(0, 19);
}

function Payload({ item }: LayoutProps) {
  return (
    <div className="workspace-payload">
      <p className="workspace-kicker">Agent output</p>
      <pre>{formatStructured(item.payload)}</pre>
    </div>
  );
}

function fieldType(item: ReviewItem): FieldValueType | null {
  const value = item.payload.value_type;
  return typeof value === "string" && FIELD_VALUE_TYPES.has(value as FieldValueType)
    ? value as FieldValueType
    : null;
}

function enumOptions(item: ReviewItem): string[] {
  const values = item.payload.allowed_values;
  return Array.isArray(values) && values.every((value) => typeof value === "string")
    ? values
    : [];
}

function FieldVerify(props: LayoutProps) {
  const path = typeof props.item.payload.path === "string"
    ? props.item.payload.path
    : "Field path unavailable";
  const type = fieldType(props.item);
  const options = enumOptions(props.item);
  const label = type === "money" ? "Corrected value (KES cents)" : "Corrected value";
  const common = {
    "aria-label": "Corrected value",
    name: "corrected",
    value: props.correctedValue,
    onChange: props.onTextChange,
    disabled: props.editBlocked !== null,
  };
  return (
    <div className="workspace-detail">
      <Payload {...props} />
      <label>
        {label}
        {type === "bool" ? (
          <select {...common}>
            <option value="true">True</option>
            <option value="false">False</option>
          </select>
        ) : type === "enum" && options.length > 0 ? (
          <select {...common}>
            {options.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        ) : type === "object" ? (
          <textarea {...common} rows={7} spellCheck={false} />
        ) : (
          <input
            {...common}
            inputMode={type === "money" ? "numeric" : undefined}
            type={type === "date" ? "date" : type === "datetime" ? "datetime-local" : "text"}
          />
        )}
      </label>
      <p>Field path: <code>{path}</code></p>
      <p>Value type: <strong>{type ?? "not supplied"}</strong></p>
      {props.editBlocked && <p className="blocked-input">Edit blocked: {props.editBlocked}</p>}
    </div>
  );
}

export const WORKSPACE_COMPONENTS = {
  field_verify: FieldVerify,
  document_classification: Payload,
  document_split: Payload,
  consistency_evidence: Payload,
  draft_release: Payload,
  mode_confirmation: Payload,
  note_review: Payload,
  pack_review: Payload,
  ex_gratia_review: Payload,
  exception_detail: Payload,
  promotion_signoff: Payload,
  sampled_output: Payload,
  paste_readback: Payload,
  partial_documents: Payload,
  kyc_verification: Payload,
  eft_match: Payload,
  reopen_prompt: Payload,
} satisfies Record<string, React.ComponentType<LayoutProps>>;

interface WorkspaceProps {
  item: ReviewItem;
  api: ConsoleApi;
  onResolved?: () => void;
}

function errorDetail(error: unknown): { code: string; detail: string } {
  if (typeof error === "object" && error !== null) {
    const value = error as Record<string, unknown>;
    if (typeof value.code === "string") {
      return {
        code: value.code,
        detail: typeof value.detail === "string" ? value.detail : "Resolution failed",
      };
    }
  }
  return { code: "RESOLUTION_FAILED", detail: "Resolution failed" };
}

function editorBlock(item: ReviewItem): string | null {
  if (item.type !== "FIELD_VERIFY") return null;
  if (item.payload.candidate_status === "blocked_on_inputs") {
    return "the private review candidate is unavailable";
  }
  if (typeof item.payload.path !== "string" || !item.payload.path.trim()) {
    return "the producing payload has no field path";
  }
  const type = fieldType(item);
  if (type === null) return "the producing payload has no recognised value_type";
  if (type === "enum" && enumOptions(item).length === 0) {
    return "the producing payload has no allowed_values enum contract";
  }
  if (type === "object") {
    const kind = item.payload.diff_kind;
    if (typeof kind !== "string" || !CHANGE_KINDS.has(kind as ChangeKind)) {
      return "object correction requires an explicit diff_kind";
    }
  }
  return null;
}

function validIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function typedCorrection(item: ReviewItem, source: string): { value: unknown; kind: ChangeKind } {
  const type = fieldType(item);
  if (type === null) throw new TypeError("A recognised value_type is required");
  if (type === "money") {
    if (!/^-?\d+$/.test(source)) throw new TypeError("Money must be integer KES cents");
    return { value: BigInt(source), kind: "money" };
  }
  if (type === "bool") {
    if (source !== "true" && source !== "false") throw new TypeError("Boolean must be true or false");
    return { value: source === "true", kind: "enum" };
  }
  if (type === "date") {
    if (!validIsoDate(source)) {
      throw new TypeError("Date must be a valid YYYY-MM-DD value");
    }
    return { value: source, kind: "date" };
  }
  if (type === "datetime") {
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(source);
    if (
      !match
      || !validIsoDate(match[1])
      || Number(match[2]) > 23
      || Number(match[3]) > 59
      || Number(match[4] ?? "0") > 59
    ) {
      throw new TypeError("Datetime must be valid");
    }
    const withSeconds = `${match[1]}T${match[2]}:${match[3]}:${match[4] ?? "00"}`;
    const parsed = new Date(`${withSeconds}+03:00`);
    if (Number.isNaN(parsed.getTime())) throw new TypeError("Datetime must be valid");
    return { value: parsed.toISOString().replace(".000Z", "Z"), kind: "date" };
  }
  if (type === "enum") {
    if (!enumOptions(item).includes(source)) throw new TypeError("Value is outside the allowed enum");
    return { value: source, kind: "enum" };
  }
  if (type === "object") {
    const value = parseLossless(source);
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      throw new TypeError("Object correction must be a JSON object");
    }
    return { value, kind: item.payload.diff_kind as ChangeKind };
  }
  return { value: source, kind: "text" };
}

// PACKET-19 §8.2/§8.3: the two approval layouts own their whole workspace —
// locked evidence, autosave, artifact viewers, and their own action payloads.
const DEDICATED_WORKSPACES: Record<
  string,
  React.ComponentType<WorkspaceProps>
> = {
  approval_note_review: ApprovalNoteWorkspace,
  approval_pack_review: ApprovalPackWorkspace,
  // PACKET-21: none of these corrects a field value, so none of them can use
  // the generic corrected-value editor.
  projection_rpa_release: ProjectionWorkspace,
  projection_divergence: ProjectionWorkspace,
  paste_readback: ProjectionWorkspace,
};

export function Workspace({ item, api, onResolved }: WorkspaceProps) {
  const Dedicated = DEDICATED_WORKSPACES[item.workspace_layout];
  if (Dedicated) return <Dedicated item={item} api={api} onResolved={onResolved} />;
  return <GenericWorkspace item={item} api={api} onResolved={onResolved} />;
}

function GenericWorkspace({ item, api, onResolved }: WorkspaceProps) {
  const Layout = (WORKSPACE_COMPONENTS as Partial<
    Record<string, React.ComponentType<LayoutProps>>
  >)[item.workspace_layout];
  const candidate = item.payload.candidate_value;
  const [correctedValue, setCorrectedValue] = useState(
    initialEditorValue(item, candidate ?? ""),
  );
  const [reason, setReason] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [error, setError] = useState<{ code: string; detail: string } | null>(null);
  const [pending, setPending] = useState(false);
  const capability = typeof item.payload.capability_id === "string"
    ? item.payload.capability_id.trim()
    : "";
  const capabilityBlocked = capability ? null : "Producing payload has no capability_id";
  const editBlocked = editorBlock(item);
  const candidateBlocked = item.payload.candidate_status === "blocked_on_inputs";

  function onTextChange(
    event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>,
  ) {
    if (event.target.name === "corrected") setCorrectedValue(event.target.value);
    else setReason(event.target.value);
  }

  async function resolve(action: ResolutionAction) {
    if (!Layout || pending) return;
    if (capabilityBlocked) {
      setError({ code: "RESOLUTION_BLOCKED_ON_INPUTS", detail: capabilityBlocked });
      return;
    }
    if (action === "reject" && !reason.trim()) {
      setRejectOpen(true);
      setError({ code: "REASON_REQUIRED", detail: "Enter a rejection reason" });
      return;
    }
    const payload: Record<string, unknown> = {
      capability_id: capability,
      diff: { typed_changes: [], prose_change_ratio: 0 },
    };
    const editExtras: Record<ReviewItem["type"], Record<string, unknown>> = {
      FIELD_VERIFY: {},
      DOC_SPLIT: { boundaries: item.payload.boundaries },
      DOC_CLASSIFY: {},
      CONSISTENCY_FLAG: {},
      DRAFT_RELEASE: {},
      MODE_CONFIRM: {},
      NOTE_REVIEW: {},
      PACK_REVIEW: {},
      EX_GRATIA: {},
      EXCEPTION: {},
      PROMOTION_SIGNOFF: {},
      SAMPLE_REVIEW: {},
      PASTE_READBACK_CHECK: {},
      PROCEED_PARTIAL: {},
      KYC_VERIFY: {},
      EFT_MATCH: {},
      REOPEN_PROMPT: {},
    };
    if (action === "edit_approve" && item.type === "FIELD_VERIFY") {
      if (editBlocked) {
        setError({ code: "RESOLUTION_BLOCKED_ON_INPUTS", detail: editBlocked });
        return;
      }
      try {
        const path = item.payload.path as string;
        const corrected = typedCorrection(item, correctedValue);
        editExtras.FIELD_VERIFY = {
          corrected_fields: { [path]: corrected.value },
          diff: {
            typed_changes: [{ path, kind: corrected.kind }],
            prose_change_ratio: 0,
          },
        };
      } catch (caught) {
        setError({
          code: "PAYLOAD_INVALID",
          detail: caught instanceof Error ? caught.message : "Correction is invalid",
        });
        return;
      }
    }
    Object.assign(
      payload,
      {
        approve: {},
        edit_approve: editExtras[item.type],
        reject: { reason: reason.trim() },
      }[action],
    );
    setPending(true);
    setError(null);
    try {
      await api.resolveReview(item.id, {
        action,
        schema_version: item.resolution_schema,
        payload,
      });
      onResolved?.();
    } catch (caught) {
      setError(errorDetail(caught));
    } finally {
      setPending(false);
    }
  }

  function onAction(event: React.MouseEvent<HTMLDivElement>) {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-action]");
    const action = button?.dataset.action;
    if (action === "approve" || action === "edit_approve" || action === "reject") {
      void resolve(action);
    }
  }

  function onWorkspaceKeyDown(event: React.KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape" && rejectOpen) {
      setRejectOpen(false);
      setError(null);
    }
  }

  return (
    <section
      className="workspace"
      aria-label={`${item.type} workspace`}
      onKeyDown={onWorkspaceKeyDown}
    >
      {!Layout ? (
        <div role="alert" className="unsupported-workspace">
          <h2>Unsupported workspace</h2>
          <p>This layout is not registered. Resolution is blocked.</p>
        </div>
      ) : (
        <Layout
          item={item}
          correctedValue={correctedValue}
          editBlocked={editBlocked}
          onTextChange={onTextChange}
        />
      )}
      {capabilityBlocked && Layout && (
        <div role="alert" className="blocked-input">
          <strong>Resolution blocked</strong>
          <span>{capabilityBlocked}. The item must be repaired by its producer.</span>
        </div>
      )}
      {rejectOpen && (
        <label className="reject-reason">
          Rejection reason · enum pending_capture
          <textarea name="reason" value={reason} onChange={onTextChange} />
        </label>
      )}
      {error && (
        <div role="alert" className="resolution-error">
          <strong>{error.code}</strong>
          <span>{error.detail}</span>
        </div>
      )}
      <div role="group" aria-label="Resolution actions" className="resolution-actions" onClick={onAction}>
        <button
          data-action="approve"
          disabled={!Layout || pending || Boolean(capabilityBlocked) || candidateBlocked}
        >
          Approve
        </button>
        <button data-action="edit_approve" disabled={!Layout || pending || Boolean(capabilityBlocked) || Boolean(editBlocked)}>
          Edit→Approve
        </button>
        <button data-action="reject" disabled={!Layout || pending || Boolean(capabilityBlocked)}>
          Reject
        </button>
      </div>
    </section>
  );
}
