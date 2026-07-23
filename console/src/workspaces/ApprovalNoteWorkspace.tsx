import React, { useCallback, useEffect, useRef, useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type {
  ApprovalNoteWorkspace as Workspace,
  AutosaveResult,
  ConsoleApi,
  NoteSlot,
  ResolutionAction,
  ReviewItem,
} from "../api/types";
import { formatEat } from "../lib/time";

type SaveState =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved"; at: string }
  | { kind: "failed"; detail: string };

interface Props {
  item: ReviewItem;
  api: ConsoleApi;
  onResolved?: () => void;
}

function slots(section: unknown): NoteSlot[] {
  return Array.isArray(section) ? (section as NoteSlot[]) : [];
}

function commentaryText(workspace: Workspace): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const slot of workspace.commentary_slots) {
    const section = workspace.current_draft.body.sections.find(
      (candidate) => candidate.template_slot === slot,
    );
    draft[slot] = typeof section?.content === "string" ? section.content : "";
  }
  return draft;
}

function LockedRows({ title, rows }: { title: string; rows: NoteSlot[] }) {
  return (
    <section aria-label={title} className="note-locked">
      <h3>{title}</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Field</th>
              <th>Value</th>
              <th>State</th>
              <th>Provenance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.slot} data-testid={`note-slot-${row.slot}`}>
                <td>{row.label}</td>
                <td>{row.display ?? "—"}</td>
                <td>
                  <span className="state-badge">{row.state}</span>
                </td>
                <td>
                  {row.source_ref ? (
                    <a
                      href={`#citation-${row.source_ref.path}`}
                      data-testid={`note-citation-${row.slot}`}
                    >
                      {`${row.citation_marker ?? ""} ${row.source_ref.path} v${row.source_ref.version}`.trim()}
                    </a>
                  ) : row.evidence && row.evidence.length > 0 ? (
                    row.evidence.map((entry) => `${entry.check_id}: ${entry.status}`).join(", ")
                  ) : (
                    <span className="availability-state">{row.blocker ?? "No provenance"}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function ApprovalNoteWorkspace({ item, api, onResolved }: Props) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [error, setError] = useState<ConsoleError | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [save, setSave] = useState<SaveState>({ kind: "idle" });
  const [recovery, setRecovery] = useState<Record<string, string> | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const dirty = useRef(false);
  const editRevision = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const workspaceRef = useRef<Workspace | null>(null);
  const saveInFlight = useRef<Promise<AutosaveResult | null> | null>(null);
  const lastSaved = useRef<AutosaveResult | null>(null);
  // The autosave timer fires outside React's render cycle, so the text it saves
  // is read from a ref rather than a closed-over render snapshot.
  const latest = useRef<Record<string, string>>({});

  const load = useCallback(async () => {
    if (!api.getApprovalNote) return;
    try {
      const value = await api.getApprovalNote(item.id);
      workspaceRef.current = value;
      setWorkspace(value);
      // After a reload or a crash the highest server version opens.
      latest.current = commentaryText(value);
      setDraft(latest.current);
      dirty.current = false;
      setError(null);
    } catch (caught) {
      setError(consoleError(caught, "The approval note workspace is unavailable."));
    }
  }, [api, item.id]);

  useEffect(() => {
    void load();
  }, [load]);

  const merged = workspace?.merged_pack.content_url ?? null;
  useEffect(() => {
    let active = true;
    if (!merged || !api.getDocument) return;
    (async () => {
      try {
        const [pdfjs, worker, bytes] = await Promise.all([
          import("pdfjs-dist"),
          import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
          api.getDocument!(merged),
        ]);
        if (!active) return;
        pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
        const document = await pdfjs.getDocument({ data: bytes }).promise;
        const page = await document.getPage(1);
        const base = page.getViewport({ scale: 1 });
        const view = page.getViewport({ scale: Math.min(1.4, 640 / base.width) });
        const element = canvas.current;
        const context = element?.getContext("2d");
        if (element && context) {
          element.width = view.width;
          element.height = view.height;
          await page.render({ canvas: element, canvasContext: context, viewport: view })
            .promise;
        }
      } catch (caught) {
        if (active) {
          setPdfError(consoleError(caught, "The merged pack could not be opened.").detail);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [api, merged]);

  const persist = useCallback(async function persistNote(): Promise<AutosaveResult | null> {
    if (saveInFlight.current) {
      await saveInFlight.current;
      return dirty.current ? persistNote() : lastSaved.current;
    }
    const current = workspaceRef.current;
    if (!api.saveApprovalNote || !current || !dirty.current) return null;
    const revision = editRevision.current;
    const body = {
      base_draft_id: current.current_draft.id,
      base_body_sha256: current.current_draft.body_sha256,
      commentary: current.commentary_slots.map((slot) => ({
        template_slot: slot,
        content: latest.current[slot] ?? "",
      })),
    };
    setSave({ kind: "saving" });
    const operation = (async (): Promise<AutosaveResult | null> => {
      try {
        const result = await api.saveApprovalNote!(
          item.id,
          body,
          `${current.current_draft.id}:${JSON.stringify(body.commentary).length}:${Date.now()}`,
        );
        lastSaved.current = result;
        // A response may only clear the exact edit revision it persisted. If
        // the officer typed during the request, the queued timer saves again.
        dirty.current = editRevision.current !== revision;
        setSave({ kind: "saved", at: formatEat(new Date().toISOString()) });
        const next = {
          ...current,
          current_draft: {
            ...current.current_draft,
            id: result.draft_id,
            version: result.version,
            body_sha256: result.body_sha256,
          },
        };
        workspaceRef.current = next;
        setWorkspace(next);
        return result;
      } catch (caught) {
        const failure = consoleError(caught, "The approval note could not be saved.");
        setSave({ kind: "failed", detail: `${failure.code}: ${failure.detail}` });
        if (failure.code === "STALE_NOTE_DRAFT") {
          // Never overwrite silently: reload the server version and keep the
          // officer's local text in a clearly labelled recovery panel.
          setRecovery({ ...latest.current });
          await load();
        }
        return null;
      }
    })();
    let tracked: Promise<AutosaveResult | null>;
    tracked = operation.finally(() => {
      if (saveInFlight.current === tracked) saveInFlight.current = null;
    });
    saveInFlight.current = tracked;
    return tracked;
  }, [api, item.id, load]);

  function edit(slot: string, value: string) {
    editRevision.current += 1;
    dirty.current = true;
    latest.current = { ...latest.current, [slot]: value };
    setDraft((current) => ({ ...current, [slot]: value }));
    if (timer.current) clearTimeout(timer.current);
    const seconds = workspace?.autosave_seconds ?? 5;
    timer.current = setTimeout(() => void persist(), seconds * 1000);
  }

  async function resolve(action: ResolutionAction) {
    if (!workspace || pending) return;
    if (action === "reject" && !reason.trim()) {
      setRejectOpen(true);
      setError({ code: "REASON_REQUIRED", detail: "Enter a rejection reason", kind: "retryable" });
      return;
    }
    setPending(true);
    setError(null);
    try {
      let signedDraft: { id: string; body_sha256: string } | null = null;
      if (action === "edit_approve") {
        // Edit→Approve autosaves first, then signs that exact saved version.
        // Prose never travels through the resolution endpoint.
        dirty.current = true;
        const saved = await persist();
        if (!saved) return;
        signedDraft = { id: saved.draft_id, body_sha256: saved.body_sha256 };
      }
      const current = signedDraft
        ? null
        : api.getApprovalNote
          ? await api.getApprovalNote(item.id)
          : workspace;
      const draftToSign = signedDraft ?? (
        current
          ? {
              id: current.current_draft.id,
              body_sha256: current.current_draft.body_sha256,
            }
          : null
      );
      if (!draftToSign) {
        throw new Error("The approval note version to sign is unavailable.");
      }
      const payload: Record<string, unknown> = {
        capability_id: "pack.note_draft",
        draft_id: draftToSign.id,
        body_sha256: draftToSign.body_sha256,
        diff: { typed_changes: [], prose_change_ratio: 0 },
      };
      if (action === "reject") payload.reason = reason.trim();
      await api.resolveReview(item.id, {
        action: action === "edit_approve" ? "approve" : action,
        schema_version: item.resolution_schema,
        payload,
      });
      onResolved?.();
    } catch (caught) {
      const failure = consoleError(caught, "The resolution was refused.");
      // Refresh first, then report: the reload must not erase the refusal the
      // officer needs to read.
      await load();
      setError(failure);
    } finally {
      setPending(false);
    }
  }

  if (error && !workspace) {
    return (
      <section role="alert" className="read-state">
        <h2>{error.code}</h2>
        <p>{error.detail}</p>
        <button onClick={() => void load()}>Retry</button>
      </section>
    );
  }
  if (!workspace) return <p className="loading-state">Loading the approval note…</p>;

  const sections = workspace.current_draft.body.sections;
  const computed = slots(
    sections.find((section) => section.template_slot === "computed")?.content,
  );
  const verification = slots(
    sections.find((section) => section.template_slot === "verification")?.content,
  );
  const rejection = workspace.current_draft.body.manager_rejection;

  return (
    <section className="approval-note" aria-label="Approval note review workspace">
      <div className="approval-note-split">
        <div className="approval-note-editor">
          <header>
            <p className="workspace-kicker">
              {`Version ${workspace.current_draft.version} · ${workspace.current_draft.status}`}
            </p>
            <h2>Approval note</h2>
          </header>
          {rejection && (
            <div role="note" data-testid="manager-rejection" className="availability-state">
              <strong>Returned by the approving manager</strong>
              <ul>
                {(rejection.reasons as Array<Record<string, string>> | undefined)?.map(
                  (entry, index) => (
                    <li key={index}>
                      {entry.field_path
                        ? `${entry.field_path}: ${entry.detail}`
                        : entry.detail}
                    </li>
                  ),
                )}
              </ul>
            </div>
          )}
          <LockedRows title="Computed and merged figures" rows={computed} />
          <LockedRows title="Verification" rows={verification} />
          <section aria-label="Commentary" className="note-commentary">
            <h3>Commentary</h3>
            {workspace.commentary_slots.map((slot) => (
              <label key={slot}>
                {slot.replaceAll("_", " ")}
                <textarea
                  aria-label={slot}
                  data-testid={`commentary-${slot}`}
                  value={draft[slot] ?? ""}
                  onBlur={() => void persist()}
                  onChange={(event) => edit(slot, event.target.value)}
                />
              </label>
            ))}
            <p data-testid="autosave-state" role="status">
              {save.kind === "saving"
                ? "Saving…"
                : save.kind === "saved"
                  ? `Saved at ${save.at} EAT`
                  : save.kind === "failed"
                    ? `Save failed — ${save.detail}`
                    : `Autosaves after ${workspace.autosave_seconds}s`}
            </p>
            {recovery && (
              <div role="alert" data-testid="recovery-panel" className="availability-state">
                <strong>Unsaved local text recovered</strong>
                <p>
                  Another tab saved a newer version. Your text was not discarded and was
                  not written over the server version.
                </p>
                {workspace.commentary_slots.map((slot) => (
                  <pre key={slot} data-testid={`recovered-${slot}`}>{recovery[slot]}</pre>
                ))}
                <button onClick={() => setRecovery(null)}>Discard recovered text</button>
              </div>
            )}
          </section>
        </div>
        <div className="approval-note-viewer">
          <h3>Merged approval pack</h3>
          {workspace.merged_pack.content_url ? (
            <canvas ref={canvas} aria-label="Merged approval pack page 1" />
          ) : (
            <p className="availability-state">No merged pack version is indexed.</p>
          )}
          {pdfError && <p role="alert">{pdfError}</p>}
          <p data-testid="icon-note-entry" className="availability-state">
            {`ICON note entry · ${workspace.icon_note_entry.status}`}
            {workspace.icon_note_entry.blocked_on
              ? ` · blocked on ${workspace.icon_note_entry.blocked_on}`
              : ""}
          </p>
        </div>
      </div>
      <div data-testid="sign-blockers" className="note-blockers">
        <h3>Blockers</h3>
        {workspace.blockers.length === 0 ? (
          <p>No blockers. This version may be signed.</p>
        ) : (
          <ul>
            {workspace.blockers.map((blocker, index) => (
              <li key={index}>{`${blocker.slot ?? "note"} · ${blocker.state}: ${blocker.detail}`}</li>
            ))}
          </ul>
        )}
      </div>
      {workspace.sign_state === "signing_pending" && (
        <p role="status" data-testid="signing-pending">
          Signing is being finalised. The resolution is durable and is not lost.
        </p>
      )}
      {rejectOpen && (
        <label className="reject-reason">
          Rejection reason · enum pending_capture
          <textarea
            aria-label="Rejection reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      )}
      {error && (
        <div role="alert" className="resolution-error" data-testid="note-resolution-error">
          <strong>{error.code}</strong>
          <span>{error.detail}</span>
        </div>
      )}
      <div role="group" aria-label="Resolution actions" className="resolution-actions">
        <button
          data-action="approve"
          disabled={pending || !workspace.signable || workspace.sign_state !== "unsigned"}
          onClick={() => void resolve("approve")}
        >
          Sign
        </button>
        <button
          data-action="edit_approve"
          disabled={pending || !workspace.signable || workspace.sign_state !== "unsigned"}
          onClick={() => void resolve("edit_approve")}
        >
          Save &amp; Sign
        </button>
        <button data-action="reject" disabled={pending} onClick={() => void resolve("reject")}>
          Reject
        </button>
      </div>
    </section>
  );
}
