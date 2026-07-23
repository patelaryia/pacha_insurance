import React, { useCallback, useEffect, useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type {
  ConsoleApi,
  PasteAssistView,
  ProjectionSummary,
  ProjectionSurface,
} from "../api/types";
import { formatKes, parseCents } from "../lib/money";
import { formatEat } from "../lib/time";

interface ProjectionSystemsProps {
  api: ConsoleApi;
  claimId: string;
}

const STATUS_LABELS: Record<ProjectionSummary["status"], string> = {
  queued: "Queued",
  executing: "In progress",
  verifying: "Verifying",
  completed: "Completed",
  failed: "Failed",
  diverged: "Diverged",
};

const AVAILABILITY_LABELS: Record<string, string> = {
  live: "Available",
  pending_capture: "Pending capture",
  blocked_on_inputs: "Blocked on inputs",
};

function humanOperation(operation: string): string {
  return operation
    .replaceAll("_", " ")
    .replace(/^(icon|edms)\./, (system) => `${system.slice(0, -1).toUpperCase()} · `);
}

/** Display-only preview. The clipboard always receives the exact server string. */
function preview(value: string, valueType: string): string {
  if (valueType !== "money") return value;
  try {
    // The strip renders shillings; the money helper reads integer cents.
    const [shillings, fraction = "00"] = value.split(".");
    return formatKes(parseCents(`${shillings}${fraction.padEnd(2, "0").slice(0, 2)}`));
  } catch {
    return value;
  }
}

export function ProjectionSystems({ api, claimId }: ProjectionSystemsProps) {
  const [surface, setSurface] = useState<ProjectionSurface | null>(null);
  const [strip, setStrip] = useState<PasteAssistView | null>(null);
  const [error, setError] = useState<ConsoleError | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [copyFailure, setCopyFailure] = useState<string | null>(null);
  const [readback, setReadback] = useState<Record<string, string>>({});
  const [attested, setAttested] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!api.getProjections) return;
    api.getProjections(claimId).then(
      (value) => setSurface(value),
      (caught) => setError(consoleError(caught, "Systems could not be read. Retry.")),
    );
  }, [api, claimId]);

  useEffect(load, [load]);

  async function guard(action: () => Promise<PasteAssistView>) {
    setBusy(true);
    setError(null);
    try {
      // The server is authoritative: its response replaces local state wholesale.
      setStrip(await action());
      load();
    } catch (caught) {
      setError(consoleError(caught, "The server refused that change."));
      if (api.getPasteAssist && strip) {
        try {
          setStrip(await api.getPasteAssist(claimId, strip.projection_id));
        } catch {
          // The refusal itself is already surfaced; keep the last server state.
        }
      }
      load();
    } finally {
      setBusy(false);
    }
  }

  async function openStrip(projectionId: string) {
    if (!api.getPasteAssist) return;
    setError(null);
    setReadback({});
    setAttested(false);
    setCopyFailure(null);
    try {
      setStrip(await api.getPasteAssist(claimId, projectionId));
    } catch (caught) {
      setError(consoleError(caught, "The paste strip could not be opened."));
    }
  }

  async function copy(label: string, value: string) {
    setCopyFailure(null);
    try {
      await navigator.clipboard.writeText(value);
      setAnnouncement(`Copied ${label}`);
    } catch {
      setAnnouncement("");
      setCopyFailure(label);
    }
  }

  async function confirm() {
    if (!api.confirmPasteAssist || !strip) return;
    setBusy(true);
    setError(null);
    try {
      await api.confirmPasteAssist(
        claimId,
        strip.projection_id,
        { attested: true, readback },
        `${strip.projection_id}:${strip.definition_version}`,
      );
      if (api.getPasteAssist) {
        setStrip(await api.getPasteAssist(claimId, strip.projection_id));
      }
    } catch (caught) {
      setError(consoleError(caught, "The confirmation was refused."));
      if (api.getPasteAssist) {
        try {
          setStrip(await api.getPasteAssist(claimId, strip.projection_id));
        } catch {
          // Keep the last server state rather than showing an optimistic result.
        }
      }
    } finally {
      setBusy(false);
      load();
    }
  }

  if (!api.getProjections) {
    return (
      <p className="availability-state">
        Projection reads are not available in this console build.
      </p>
    );
  }
  if (!surface) {
    return <p className="loading-state">Loading system projections…</p>;
  }

  const readbackReady = (strip?.readback_fields ?? []).every(
    (field) =>
      field.format_status === "live" &&
      (!field.required || (readback[field.path] ?? "").trim().length > 0),
  );
  const groupsReady = (strip?.groups ?? []).every((group) => group.done);
  const stripComplete = strip?.status === "completed";

  return (
    <div className="projection-systems">
      {error && (
        <p role="alert" className="read-state read-state-retryable">
          {error.code}: {error.detail}
        </p>
      )}
      <section aria-label="Registered operations">
        <p className="workspace-kicker">Target systems</p>
        <h3>Registered operations</h3>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Operation</th>
                <th>Capability</th>
                <th>Mode</th>
                <th>Availability</th>
                <th>Blocker</th>
                <th>Owner</th>
              </tr>
            </thead>
            <tbody>
              {surface.operations.map((operation) => (
                <tr key={operation.id}>
                  <td>{humanOperation(operation.id)}</td>
                  <td><code>{operation.capability_id}</code></td>
                  <td>{operation.mode.replaceAll("_", " ")}</td>
                  <td>{AVAILABILITY_LABELS[operation.status] ?? operation.status}</td>
                  <td>{operation.blocked_on ?? "None"}</td>
                  <td>{operation.owner_prd}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-label="Claim projections">
        <h3>Claim projections</h3>
        {surface.projections.length === 0 ? (
          <p className="empty-state">
            No projection has been requested for this claim. Every registered
            operation above is pending capture or blocked on inputs.
          </p>
        ) : (
          <ul className="record-grid">
            {surface.projections.map((projection) => (
              <li key={projection.id}>
                <article>
                  <h4>{humanOperation(projection.operation)}</h4>
                  <dl className="fact-list">
                    <div>
                      <dt>Status</dt>
                      <dd>{STATUS_LABELS[projection.status]}</dd>
                    </div>
                    <div>
                      <dt>Definition version</dt>
                      <dd>{projection.definition_version ?? "Unavailable"}</dd>
                    </div>
                    <div>
                      <dt>Snapshot hash</dt>
                      <dd><code>{projection.snapshot_hash ?? "Unavailable"}</code></dd>
                    </div>
                    <div>
                      <dt>Screen progress</dt>
                      <dd>
                        {Object.keys(projection.groups_done).length === 0
                          ? "Not started"
                          : `${
                            Object.values(projection.groups_done).filter(Boolean).length
                          } of ${Object.keys(projection.groups_done).length} screens done`}
                      </dd>
                    </div>
                    <div>
                      <dt>Readback fields</dt>
                      <dd>
                        {projection.readback_paths.length === 0
                          ? "None declared"
                          : projection.readback_paths.join(", ")}
                      </dd>
                    </div>
                    <div>
                      <dt>Attested by</dt>
                      <dd>
                        {projection.attested_by === null
                          ? "Not attested"
                          : `${projection.attested_by} · ${
                            projection.attested_at
                              ? formatEat(projection.attested_at)
                              : "Time unavailable"
                          }`}
                      </dd>
                    </div>
                    <div>
                      <dt>Paste time</dt>
                      <dd>
                        {projection.paste_seconds === null
                          ? "Not measured"
                          : `${projection.paste_seconds} seconds`}
                      </dd>
                    </div>
                  </dl>
                  <button
                    onClick={() => void openStrip(projection.id)}
                    aria-label={`Open paste strip for ${projection.operation}`}
                  >
                    Open paste strip
                  </button>
                </article>
              </li>
            ))}
          </ul>
        )}
      </section>

      {strip && (
        <section aria-label="Paste assist strip" className="paste-strip">
          <h3>Paste assist — {humanOperation(strip.operation)}</h3>
          <p>
            Definition {strip.definition_version} ·{" "}
            {STATUS_LABELS[strip.status]} ·{" "}
            {strip.elapsed_seconds === null
              ? "Clock not started"
              : `${strip.elapsed_seconds} seconds elapsed`}
          </p>
          {strip.status === "queued" && (
            <button
              disabled={busy}
              onClick={() =>
                api.startPasteAssist
                  ? void guard(() => api.startPasteAssist!(claimId, strip.projection_id))
                  : undefined}
            >
              Start paste assist
            </button>
          )}
          <p aria-live="polite" className="live-region">{announcement}</p>
          {copyFailure && (
            <p role="alert">
              Copy failed for {copyFailure}. The field is unchanged — copy it manually.
            </p>
          )}
          {strip.groups.map((group) => (
            <fieldset key={group.id}>
              <legend>{group.label}</legend>
              <ul className="paste-field-list">
                {group.fields.map((field) => (
                  <li key={field.step_id}>
                    <span>{field.label}</span>
                    <strong>{preview(field.copy_value, field.value_type)}</strong>
                    <small>version {String(field.field_version)}</small>
                    <button
                      aria-label={`Copy ${field.label}`}
                      onClick={() => void copy(field.label, field.copy_value)}
                    >
                      Copy
                    </button>
                  </li>
                ))}
              </ul>
              <label>
                <input
                  type="checkbox"
                  checked={group.done}
                  disabled={busy || strip.status !== "executing"}
                  onChange={(event) =>
                    api.setPasteGroup
                      ? void guard(() =>
                        api.setPasteGroup!(
                          claimId,
                          strip.projection_id,
                          group.id,
                          event.target.checked,
                        ))
                      : undefined}
                />
                {group.label} entered
              </label>
            </fieldset>
          ))}
          {strip.readback_fields.length > 0 && (
            <fieldset>
              <legend>Readback</legend>
              {strip.readback_fields.map((field) => (
                <label key={field.path}>
                  {field.label}
                  <input
                    type="text"
                    value={readback[field.path] ?? ""}
                    disabled={field.format_status !== "live" || stripComplete}
                    onChange={(event) =>
                      setReadback((current) => ({
                        ...current,
                        [field.path]: event.target.value,
                      }))}
                  />
                  {field.format_status !== "live" && (
                    <small>
                      Format pending capture{field.blocked_on ? ` — ${field.blocked_on}` : ""}
                    </small>
                  )}
                </label>
              ))}
            </fieldset>
          )}
          <label>
            <input
              type="checkbox"
              checked={attested}
              disabled={stripComplete}
              onChange={(event) => setAttested(event.target.checked)}
            />
            {strip.attestation_text}
          </label>
          <button
            disabled={
              busy
              || stripComplete
              || strip.status !== "executing"
              || !groupsReady
              || !readbackReady
              || !attested
            }
            onClick={() => void confirm()}
          >
            Confirm entry
          </button>
          {stripComplete && (
            <p className="availability-state">
              This projection is complete and immutable.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
