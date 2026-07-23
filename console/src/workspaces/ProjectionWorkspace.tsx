import React, { useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type { ConsoleApi, ResolutionAction, ReviewItem } from "../api/types";

interface ProjectionWorkspaceProps {
  item: ReviewItem;
  api: ConsoleApi;
  onResolved?: () => void;
}

const DISPOSITIONS = [
  "target_out_of_band",
  "platform_snapshot_wrong",
  "target_readback_wrong",
  "unresolved",
] as const;

const EMPTY_DIFF = { typed_changes: [] as unknown[], prose_change_ratio: 0 };

function text(value: unknown, fallback = "Unavailable"): string {
  return typeof value === "string" && value ? value : fallback;
}

function mismatchPaths(item: ReviewItem): Array<Record<string, unknown>> {
  const paths = item.payload.paths;
  return Array.isArray(paths) ? (paths as Array<Record<string, unknown>>) : [];
}

/**
 * PACKET-21 §6/§10/§12. One dedicated workspace for the three projection
 * layouts. Each builds its own resolution payload, because none of them
 * corrects a field value: an RPA release launches an exact projection, a
 * divergence records a disposition, and a sampled readback resolves against an
 * opaque capture id. No target value is ever posted back.
 */
export function ProjectionWorkspace(props: ProjectionWorkspaceProps) {
  const { item, api, onResolved } = props;
  const [error, setError] = useState<ConsoleError | null>(null);
  const [pending, setPending] = useState(false);
  const [reason, setReason] = useState("");
  const [disposition, setDisposition] = useState<string>(DISPOSITIONS[3]);
  const [observed, setObserved] = useState<Record<string, string>>({});
  const [capture, setCapture] = useState<
    { capture_id: string; mismatch_paths: string[] } | null
  >(null);

  const capability = text(item.payload.capability_id, "");

  async function resolve(
    action: ResolutionAction,
    schema: string,
    payload: Record<string, unknown>,
  ) {
    setPending(true);
    setError(null);
    try {
      await api.resolveReview(item.id, {
        action,
        schema_version: schema,
        payload: { capability_id: capability, ...payload },
      });
      onResolved?.();
    } catch (caught) {
      setError(consoleError(caught, "The server refused that resolution."));
    } finally {
      setPending(false);
    }
  }

  const banner = error && (
    <p role="alert" className="read-state read-state-retryable">
      {error.code}: {error.detail}
    </p>
  );

  if (item.workspace_layout === "projection_rpa_release") {
    const action = (item.payload.action ?? {}) as Record<string, unknown>;
    const staged = (action.payload ?? {}) as Record<string, unknown>;
    const exact = {
      projection_id: text(staged.projection_id, ""),
      definition_version: text(staged.definition_version, ""),
      snapshot_hash: text(staged.snapshot_hash, ""),
    };
    return (
      <section className="workspace-detail" data-testid="projection-rpa-release">
        {banner}
        <h3>Confirm robotic entry</h3>
        <dl className="fact-list">
          <div><dt>Operation</dt><dd>{text(staged.operation)}</dd></div>
          <div><dt>Definition version</dt><dd>{exact.definition_version}</dd></div>
          <div>
            <dt>Snapshot hash</dt>
            <dd><code data-testid="projection-snapshot-hash">{exact.snapshot_hash}</code></dd>
          </div>
          <div><dt>Capability</dt><dd><code>{capability}</code></dd></div>
        </dl>
        <p className="availability-state">
          Approving launches exactly this projection. Nothing else is sent, and
          no target value is shown here.
        </p>
        <button
          data-testid="projection-approve"
          disabled={pending || !capability}
          onClick={() => void resolve("approve", "DRAFT_RELEASE_PROJECTION@1", {
            ...exact,
            diff: EMPTY_DIFF,
          })}
        >
          Approve and launch
        </button>
        <button
          data-testid="projection-fallback"
          disabled={pending || !capability}
          onClick={() => void resolve("edit_approve", "DRAFT_RELEASE_PROJECTION@1", {
            ...exact,
            diff: {
              typed_changes: [{ path: "projection.mode", kind: "enum", to: "paste_assist" }],
              prose_change_ratio: 0,
            },
          })}
        >
          Switch this row to paste assist
        </button>
        <label>
          Reason for rejection
          <input
            data-testid="projection-reject-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button
          data-testid="projection-reject"
          disabled={pending || !capability || reason.trim().length === 0}
          onClick={() => void resolve("reject", "DRAFT_RELEASE_PROJECTION@1", {
            ...exact,
            diff: EMPTY_DIFF,
            reason,
          })}
        >
          Reject
        </button>
      </section>
    );
  }

  if (item.workspace_layout === "projection_divergence") {
    const paths = mismatchPaths(item);
    return (
      <section className="workspace-detail" data-testid="projection-divergence">
        {banner}
        <h3>Reconciliation divergence</h3>
        <p>
          Detected by {text(item.payload.detected_by)}. The claim value and the
          target value are both unchanged; recording a disposition corrects
          neither side.
        </p>
        <div className="table-scroll">
          <table>
            <thead>
              <tr><th>Path</th><th>Kind</th><th>Expected hash</th><th>Actual hash</th></tr>
            </thead>
            <tbody>
              {paths.map((row) => (
                <tr key={String(row.path)} data-testid={`divergence-path-${String(row.path)}`}>
                  <td>{String(row.path)}</td>
                  <td>{String(row.kind)}</td>
                  <td><code>{String(row.expected_sha256).slice(0, 12)}</code></td>
                  <td><code>{String(row.actual_sha256).slice(0, 12)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <label>
          Disposition
          <select
            data-testid="divergence-disposition"
            value={disposition}
            onChange={(event) => setDisposition(event.target.value)}
          >
            {DISPOSITIONS.map((value) => (
              <option key={value} value={value}>{value.replaceAll("_", " ")}</option>
            ))}
          </select>
        </label>
        <button
          data-testid="divergence-resolve"
          disabled={pending || !capability}
          onClick={() => void resolve("approve", "EXCEPTION_DIVERGENCE@1", {
            disposition,
            diff: EMPTY_DIFF,
          })}
        >
          Record disposition
        </button>
      </section>
    );
  }

  const declared = Array.isArray(item.payload.readback_paths)
    ? (item.payload.readback_paths as string[])
    : [];
  return (
    <section className="workspace-detail" data-testid="paste-readback">
      {banner}
      <h3>Sampled readback check</h3>
      <p>
        Type exactly what the target screen shows. The value is protected before
        it is stored and never enters this review’s history.
      </p>
      {declared.map((path) => (
        <label key={path}>
          {path}
          <input
            data-testid={`paste-readback-${path}`}
            value={observed[path] ?? ""}
            onChange={(event) =>
              setObserved((current) => ({ ...current, [path]: event.target.value }))}
          />
        </label>
      ))}
      <button
        data-testid="paste-readback-capture"
        disabled={pending || !api.capturePasteReadback
          || declared.some((path) => (observed[path] ?? "").trim().length === 0)}
        onClick={() => {
          setPending(true);
          setError(null);
          void api.capturePasteReadback!(item.id, observed)
            .then((result) => setCapture(result))
            .catch((caught) =>
              setError(consoleError(caught, "The capture was refused.")))
            .finally(() => setPending(false));
        }}
      >
        Capture observed values
      </button>
      {capture && (
        <div data-testid="paste-readback-result">
          <p>
            Capture <code>{capture.capture_id}</code> ·{" "}
            {capture.mismatch_paths.length === 0
              ? "the server comparison is exact"
              : `${capture.mismatch_paths.length} path(s) differ`}
          </p>
          <button
            data-testid="paste-readback-approve"
            disabled={pending || capture.mismatch_paths.length > 0}
            onClick={() => void resolve("approve", "PASTE_READBACK_CHECK@2", {
              capture_id: capture.capture_id,
              diff: EMPTY_DIFF,
            })}
          >
            Approve — values match
          </button>
          <button
            data-testid="paste-readback-diverge"
            disabled={pending || capture.mismatch_paths.length === 0}
            onClick={() => void resolve("edit_approve", "PASTE_READBACK_CHECK@2", {
              capture_id: capture.capture_id,
              diff: {
                typed_changes: capture.mismatch_paths.map((path) => ({ path, kind: "text" })),
                prose_change_ratio: 0,
              },
            })}
          >
            Record divergence
          </button>
        </div>
      )}
      <label>
        Reason the target could not be read
        <input
          data-testid="paste-readback-reason"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <button
        data-testid="paste-readback-reject"
        disabled={pending || reason.trim().length === 0}
        onClick={() => void resolve("reject", "PASTE_READBACK_CHECK@2", {
          capture_id: capture?.capture_id ?? "",
          diff: EMPTY_DIFF,
          reason,
        })}
      >
        Reject
      </button>
    </section>
  );
}
