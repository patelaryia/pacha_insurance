import React, { useCallback, useEffect, useMemo, useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type { ConsoleApi, PackGeneration, PackReadiness } from "../api/types";

const UPLOAD_ITEMS = new Set(["assessor_payment_request", "claim_details_report"]);
const STATE_LABEL: Record<string, string> = {
  ready: "Resolved",
  ambiguous: "Ambiguous",
  missing: "Missing",
  invalid: "Invalid",
  pending_integration: "Pending integration",
};

interface SourceOption {
  kind: "document" | "communication";
  id: string;
  label: string;
}

interface Props {
  api: ConsoleApi;
  claimId: string;
  /** Same-claim documents already returned by Claim 360. */
  documents: Array<Record<string, unknown>>;
  /** Same-claim communications already returned by Claim 360. */
  communications: Array<Record<string, unknown>>;
}

function text(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function options(
  documents: Array<Record<string, unknown>>,
  communications: Array<Record<string, unknown>>,
): SourceOption[] {
  // Selection is offered only from what Claim 360 already returned for this
  // claim, so a cross-claim id can never be typed into the card.
  return [
    ...documents
      .filter((row) => typeof row.id === "string")
      .map((row) => ({
        kind: "document" as const,
        id: String(row.id),
        label: text(row.filename, String(row.id)),
      })),
    ...communications
      .filter((row) => typeof row.id === "string")
      .map((row) => ({
        kind: "communication" as const,
        id: String(row.id),
        label: text(row.subject, String(row.id)),
      })),
  ];
}

export function ApprovalPackReadiness({ api, claimId, documents, communications }: Props) {
  const [card, setCard] = useState<PackReadiness | null>(null);
  const [error, setError] = useState<ConsoleError | null>(null);
  const [generation, setGeneration] = useState<PackGeneration | null>(null);
  const [generationError, setGenerationError] = useState<ConsoleError | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<{ item: string; detail: string } | null>(
    null,
  );

  const sources = useMemo(
    () => options(documents, communications),
    [documents, communications],
  );

  const load = useCallback(async () => {
    if (!api.getPackReadiness) return;
    try {
      setCard(await api.getPackReadiness(claimId));
      setError(null);
    } catch (caught) {
      setError(consoleError(caught, "The approval-pack readiness card is unavailable."));
    }
  }, [api, claimId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!api.getPackReadiness) {
    return (
      <p className="availability-state">
        The approval-pack surface is not installed in this deployment.
      </p>
    );
  }

  if (error && !card) {
    return (
      <section role="alert" className="read-state" data-testid="readiness-error">
        <h2>{error.code}</h2>
        <p>{error.detail}</p>
        <button onClick={() => void load()}>Retry</button>
      </section>
    );
  }

  if (!card) return <p className="loading-state">Loading the approval-pack manifest…</p>;

  async function select(itemId: string, value: string) {
    if (!api.selectPackSources || !value) return;
    const [kind, id] = value.split(":");
    try {
      await api.selectPackSources(claimId, itemId, [{ kind, id }]);
      await load();
    } catch (caught) {
      setError(consoleError(caught, "The source selection was refused."));
    }
  }

  async function upload(itemId: string, file: File | undefined) {
    if (!api.uploadPackItem || !file) return;
    setUploading(itemId);
    setUploadError(null);
    try {
      await api.uploadPackItem(claimId, itemId, file);
      await load();
    } catch (caught) {
      const detail = consoleError(caught, "The upload was refused.");
      setUploadError({ item: itemId, detail: `${detail.code}: ${detail.detail}` });
    } finally {
      setUploading(null);
    }
  }

  async function generate() {
    if (!api.generatePack || !card || busy) return;
    setBusy(true);
    setGenerationError(null);
    try {
      // The fingerprint pins the exact card the officer read; the key makes the
      // request replayable without ever producing a second pack version.
      const result = await api.generatePack(
        claimId,
        { readiness_fingerprint: card.fingerprint },
        `${claimId}:${card.fingerprint}`,
      );
      setGeneration(result);
    } catch (caught) {
      const failure = consoleError(caught, "Generation was refused.");
      setGenerationError(failure);
      // A stale fingerprint means the card the officer read no longer holds.
      // Refresh it and never report success.
      setGeneration(null);
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="readiness-card" aria-label="Approval pack readiness">
      <header>
        <p className="workspace-kicker">PRD-08 · Approval pack</p>
        <h2>Manifest readiness</h2>
        <p data-testid="readiness-state">
          {card.ready
            ? "All 13 manifest items are resolved."
            : `${card.blockers.length} blocker(s) prevent generation.`}
        </p>
      </header>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Item</th>
              <th>State</th>
              <th>Sources</th>
              <th>Blockers</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {card.items.map((item) => (
              <tr key={item.id} data-testid={`readiness-row-${item.id}`}>
                <td>{item.order}</td>
                <td>{item.label}</td>
                <td>
                  <span className="state-badge">
                    {STATE_LABEL[item.state] ?? item.state}
                  </span>
                </td>
                <td>
                  {item.sources.length === 0
                    ? "None resolved"
                    : item.sources.map((source) => source.filename).join(", ")}
                </td>
                <td>
                  {item.blockers.length === 0
                    ? "—"
                    : item.blockers
                        .map((blocker) => `${blocker.code}: ${blocker.detail}`)
                        .join("; ")}
                </td>
                <td>
                  {UPLOAD_ITEMS.has(item.id) ? (
                    <label>
                      <span className="visually-hidden">{`Upload ${item.label}`}</span>
                      <input
                        type="file"
                        accept="application/pdf"
                        aria-label={`Upload ${item.label}`}
                        disabled={uploading !== null}
                        onChange={(event) =>
                          void upload(item.id, event.target.files?.[0])}
                      />
                      {uploading === item.id && (
                        <span role="status" data-testid={`upload-progress-${item.id}`}>
                          Uploading…
                        </span>
                      )}
                      {uploadError?.item === item.id && (
                        <span role="alert" data-testid={`upload-error-${item.id}`}>
                          {uploadError.detail}
                        </span>
                      )}
                    </label>
                  ) : item.state === "ready" ? (
                    "—"
                  ) : (
                    <label>
                      <span className="visually-hidden">{`Select ${item.label}`}</span>
                      <select
                        aria-label={`Select ${item.label}`}
                        defaultValue=""
                        onChange={(event) => void select(item.id, event.target.value)}
                      >
                        <option value="">Choose a claim source…</option>
                        {sources.map((source) => (
                          <option
                            key={`${source.kind}:${source.id}`}
                            value={`${source.kind}:${source.id}`}
                          >
                            {`${source.kind}: ${source.label}`}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="readiness-actions">
        <button
          data-testid="generate-pack"
          disabled={!card.ready || busy}
          onClick={() => void generate()}
        >
          {busy ? "Generating…" : "Generate approval pack"}
        </button>
        {generationError && (
          <p role="alert" data-testid="generation-error">
            <strong>{generationError.code}</strong> {generationError.detail}
          </p>
        )}
        {generation && (
          <p data-testid="generation-outcome">
            {/* The server outcome is reported verbatim: staged is not success. */}
            {generation.status === "staged"
              ? `Staged for release · ${generation.capability_id}`
              : `${generation.status} · note ${generation.note_status ?? "unknown"}`}
          </p>
        )}
      </div>
    </section>
  );
}
