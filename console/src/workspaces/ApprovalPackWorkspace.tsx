import React, { useEffect, useRef, useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type { ConsoleApi, ResolutionAction, ReviewItem } from "../api/types";
import { formatKes, parseCents } from "../lib/money";

interface Props {
  item: ReviewItem;
  api: ConsoleApi;
  onResolved?: () => void;
}

interface ArtifactPane {
  label: string;
  url: string | null;
  testId: string;
}

function stringField(item: ReviewItem, key: string): string | null {
  const value = item.payload[key];
  return typeof value === "string" && value ? value : null;
}

function provenanceLabel(item: ReviewItem): string {
  const provenance = item.payload.route_provenance;
  if (typeof provenance !== "object" || provenance === null) {
    return "Route provenance unavailable";
  }
  const row = provenance as Record<string, unknown>;
  if (row.source === "calc") {
    return `calc ${row.calc_id} v${row.calc_version} · run ${row.calc_run_id}`;
  }
  return `field ${row.path} v${row.field_version} · ${row.blocked_calc_id} is ${row.blocked_calc_status}`;
}

function sideEffects(item: ReviewItem): Array<Record<string, unknown>> {
  const value = item.payload.side_effects;
  return Array.isArray(value) ? (value as Array<Record<string, unknown>>) : [];
}

function ArtifactCanvas({ api, pane }: { api: ConsoleApi; pane: ArtifactPane }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    if (!pane.url || !api.getDocument) return;
    (async () => {
      try {
        const [pdfjs, worker, bytes] = await Promise.all([
          import("pdfjs-dist"),
          import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
          api.getDocument!(pane.url!),
        ]);
        if (!active) return;
        pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
        const document = await pdfjs.getDocument({ data: bytes }).promise;
        const page = await document.getPage(1);
        const base = page.getViewport({ scale: 1 });
        const view = page.getViewport({ scale: Math.min(1.2, 560 / base.width) });
        const element = canvas.current;
        const context = element?.getContext("2d");
        if (element && context) {
          element.width = view.width;
          element.height = view.height;
          await page.render({ canvas: element, canvasContext: context, viewport: view })
            .promise;
        }
      } catch (caught) {
        if (active) setError(consoleError(caught, "The artifact could not be opened.").detail);
      }
    })();
    return () => {
      active = false;
    };
  }, [api, pane.url]);

  return (
    <figure data-testid={pane.testId}>
      <figcaption>{pane.label}</figcaption>
      {pane.url ? (
        <canvas ref={canvas} aria-label={pane.label} />
      ) : (
        <p className="availability-state">{`${pane.label} is not indexed for this claim.`}</p>
      )}
      {error && <p role="alert">{error}</p>}
    </figure>
  );
}

export function ApprovalPackWorkspace({ item, api, onResolved }: Props) {
  const [annotation, setAnnotation] = useState("");
  const [reasons, setReasons] = useState<
    Array<{ code: string; detail: string; field_path: string }>
  >([{ code: "", detail: "", field_path: "" }]);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [error, setError] = useState<ConsoleError | null>(null);
  const [pending, setPending] = useState(false);
  const [resolved, setResolved] = useState(false);

  const claimId = item.claim_id;
  const mergedEventId = stringField(item, "merged_event_id");
  const signedEventId = stringField(item, "note_signed_event_id");
  const amount = item.payload.routing_amount_cents;
  const alerts = sideEffects(item);

  function payload(action: ResolutionAction): Record<string, unknown> {
    const base: Record<string, unknown> = {
      capability_id: "pack.route",
      merged_event_id: item.payload.merged_event_id,
      note_signed_event_id: item.payload.note_signed_event_id,
      draft_id: item.payload.draft_id,
      body_sha256: item.payload.body_sha256,
      routing_amount_cents: item.payload.routing_amount_cents,
      required_role: item.payload.required_role,
      diff: { typed_changes: [], prose_change_ratio: 0 },
    };
    if (action === "edit_approve") base.annotation = annotation.trim();
    if (action === "reject") {
      const structured = reasons
        .filter((row) => row.code.trim() && row.detail.trim())
        .map((row) =>
          row.field_path.trim()
            ? { code: row.code.trim(), detail: row.detail.trim(), field_path: row.field_path.trim() }
            : { code: row.code.trim(), detail: row.detail.trim() },
        );
      base.reasons = structured;
      base.reason = structured.map((row) => row.detail).join(" ");
      // A named corrected path must also appear in the typed diff so PRD-03
      // captures a complete correction case rather than a fabricated one.
      base.diff = {
        typed_changes: structured
          .filter((row) => "field_path" in row)
          .map((row) => ({ path: (row as { field_path: string }).field_path, kind: "text" })),
        prose_change_ratio: 0,
      };
    }
    return base;
  }

  async function resolve(action: ResolutionAction) {
    if (pending) return;
    if (action === "edit_approve" && !annotation.trim()) {
      setError({
        code: "PAYLOAD_INVALID",
        detail: "Annotate & Approve requires a manager annotation",
        kind: "retryable",
      });
      return;
    }
    if (action === "reject") {
      const complete = reasons.some((row) => row.code.trim() && row.detail.trim());
      if (!complete) {
        setRejectOpen(true);
        setError({
          code: "REASON_REQUIRED",
          detail: "Enter at least one structured rejection reason",
          kind: "retryable",
        });
        return;
      }
    }
    setPending(true);
    setError(null);
    try {
      await api.resolveReview(item.id, {
        action,
        schema_version: item.resolution_schema,
        payload: payload(action),
      });
      // Only a committed server resolution removes the item from the queue.
      setResolved(true);
      onResolved?.();
    } catch (caught) {
      setError(consoleError(caught, "The approval was refused."));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="approval-pack" aria-label="Approval pack workspace">
      <header>
        <p className="workspace-kicker">S-3 · {String(item.payload.required_role ?? "role unknown")}</p>
        <h2>Approval pack</h2>
        <dl>
          <div>
            <dt>Routing amount</dt>
            <dd data-testid="routing-amount">
              {typeof amount === "bigint" || typeof amount === "number" || typeof amount === "string"
                ? formatKes(parseCents(amount as string | bigint))
                : "Amount unavailable"}
            </dd>
          </div>
          <div>
            <dt>Provenance</dt>
            <dd data-testid="route-provenance">{provenanceLabel(item)}</dd>
          </div>
        </dl>
      </header>
      <div className="approval-pack-split">
        <ArtifactCanvas
          api={api}
          pane={{
            label: "Merged approval pack",
            testId: "merged-pack-pane",
            url:
              claimId && mergedEventId
                ? `/claims/${encodeURIComponent(claimId)}/approval-pack/artifacts/${encodeURIComponent(mergedEventId)}`
                : null,
          }}
        />
        <ArtifactCanvas
          api={api}
          pane={{
            label: "Signed approval note",
            testId: "signed-note-pane",
            url:
              claimId && signedEventId
                ? `/claims/${encodeURIComponent(claimId)}/approval-pack/artifacts/${encodeURIComponent(signedEventId)}`
                : null,
          }}
        />
      </div>
      <p data-testid="t03-state" className="availability-state">
        {alerts.length === 0
          ? "T-03 alert · not required for this band"
          : `T-03 alert · rendered (${alerts
              .map((row) => String(row.template_id))
              .join(", ")})`}
      </p>
      <label>
        Manager annotation
        <textarea
          aria-label="Manager annotation"
          data-testid="manager-annotation"
          value={annotation}
          onChange={(event) => setAnnotation(event.target.value)}
        />
      </label>
      {rejectOpen && (
        <fieldset data-testid="rejection-reasons">
          <legend>Rejection reasons · enum pending_capture</legend>
          {reasons.map((row, index) => (
            <div key={index}>
              <label>
                Reason code
                <input
                  aria-label={`Reason ${index + 1} code`}
                  value={row.code}
                  onChange={(event) =>
                    setReasons((current) =>
                      current.map((entry, position) =>
                        position === index ? { ...entry, code: event.target.value } : entry,
                      ),
                    )}
                />
              </label>
              <label>
                Detail
                <textarea
                  aria-label={`Reason ${index + 1} detail`}
                  value={row.detail}
                  onChange={(event) =>
                    setReasons((current) =>
                      current.map((entry, position) =>
                        position === index ? { ...entry, detail: event.target.value } : entry,
                      ),
                    )}
                />
              </label>
              <label>
                Corrected field path (optional)
                <input
                  aria-label={`Reason ${index + 1} field path`}
                  value={row.field_path}
                  onChange={(event) =>
                    setReasons((current) =>
                      current.map((entry, position) =>
                        position === index
                          ? { ...entry, field_path: event.target.value }
                          : entry,
                      ),
                    )}
                />
              </label>
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              setReasons((current) => [...current, { code: "", detail: "", field_path: "" }])}
          >
            Add another reason
          </button>
        </fieldset>
      )}
      {error && (
        <div role="alert" className="resolution-error" data-testid="approval-error">
          <strong>{error.code}</strong>
          <span>{error.detail}</span>
        </div>
      )}
      {resolved && <p role="status" data-testid="approval-resolved">Resolution committed.</p>}
      <div role="group" aria-label="Resolution actions" className="resolution-actions">
        <button data-action="approve" disabled={pending} onClick={() => void resolve("approve")}>
          Approve
        </button>
        <button
          data-action="edit_approve"
          disabled={pending}
          onClick={() => void resolve("edit_approve")}
        >
          Annotate &amp; Approve
        </button>
        <button
          data-action="reject"
          disabled={pending}
          onClick={() => {
            setRejectOpen(true);
            void resolve("reject");
          }}
        >
          Reject
        </button>
      </div>
    </section>
  );
}
