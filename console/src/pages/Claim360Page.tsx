import React, { useEffect, useRef, useState } from "react";

import { consoleError, type ConsoleError } from "../api/errors";
import type { Citation, Claim360, ConsoleApi, ReviewItem } from "../api/types";
import { ApprovalPackReadiness } from "../components/ApprovalPackReadiness";
import { CitationOverlay } from "../components/CitationOverlay";
import { ProjectionSystems } from "../components/ProjectionSystems";
import { formatStructured } from "../lib/json";
import { formatKes, parseCents } from "../lib/money";
import { formatEat } from "../lib/time";
import { Workspace } from "../workspaces/registry";

const CLAIM_STATES = [
  "INTIMATED", "TRIAGED", "AWAITING_DOCS", "IN_ASSESSMENT", "REPORT_RECEIVED",
  "REGISTERED", "RESERVED", "PACK_READY", "IN_APPROVAL", "APPROVED", "IN_REPAIR",
  "REINSPECTION", "RELEASED", "WRITE_OFF", "SALVAGE_BIDDING", "CLIENT_ELECTION",
  "SURRENDER_CHECKLIST", "RETAINED", "SETTLEMENT", "SETTLED", "CLOSED", "DECLINED",
  "WITHDRAWN", "VOID",
] as const;

const TABS = [
  "Overview", "Documents", "Fields & Citations", "Financials", "Timeline", "Systems",
  "Communications",
] as const;

interface Claim360PageProps { api: ConsoleApi; claimId: string; }

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function humanLabel(value: string): string {
  return value.replaceAll("_", " ").replaceAll(".", " · ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Unavailable";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.length === 0 ? "None" : value.map(displayValue).join(", ");
  return Object.entries(record(value))
    .map(([key, nested]) => `${humanLabel(key)}: ${displayValue(nested)}`)
    .join("; ");
}

function fieldValue(value: unknown, valueType: string): string {
  if (valueType === "money") {
    try {
      return formatKes(parseCents(value as string | bigint));
    } catch {
      return "Invalid money value";
    }
  }
  return displayValue(value);
}

function Facts({ value, omit = [] }: { value: Record<string, unknown>; omit?: string[] }) {
  const entries = Object.entries(value).filter(([key]) => !omit.includes(key));
  if (entries.length === 0) return <p className="empty-state">No detail committed.</p>;
  return (
    <dl className="fact-list">
      {entries.map(([key, detail]) => (
        <div key={key}>
          <dt>{humanLabel(key)}</dt>
          <dd>{displayValue(detail)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ReadError({ error, onRetry }: { error: ConsoleError; onRetry: () => void }) {
  return (
    <section role="alert" className={`read-state read-state-${error.kind}`}>
      <p className="workspace-kicker">
        {error.kind === "authentication" ? "Authentication required" : error.kind === "authorisation" ? "Access denied" : "Read interrupted"}
      </p>
      <h1>{error.code}</h1>
      <p>{error.detail}</p>
      <p>
        {error.kind === "authentication"
          ? "Refresh your Microsoft sign-in, then retry."
          : error.kind === "authorisation"
            ? "Ask an administrator to verify your immutable identity mapping and claims role."
            : "No claim data was changed."}
      </p>
      {error.kind !== "authorisation" && <button onClick={onRetry}>Retry</button>}
    </section>
  );
}

export function Claim360Page({ api, claimId }: Claim360PageProps) {
  const [claim, setClaim] = useState<Claim360 | null>(null);
  const [activeTab, setActiveTab] = useState<(typeof TABS)[number]>("Overview");
  const [reopenNotice, setReopenNotice] = useState(false);
  const [error, setError] = useState<ConsoleError | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);
  const [fieldReview, setFieldReview] = useState<ReviewItem | null>(null);
  const [calcFocus, setCalcFocus] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [citationViewport, setCitationViewport] = useState<{
    width: number;
    height: number;
    rotation: 0 | 90 | 180 | 270;
  } | null>(null);
  const citationCanvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let current = true;
    setError(null);
    api.getClaim360(claimId).then(
      (value) => { if (current) setClaim(value); },
      (caught) => { if (current) setError(consoleError(caught, "Claim 360 is unavailable. Retry the read.")); },
    );
    return () => { current = false; };
  }, [api, claimId, reload]);

  async function openCitation(fieldPath: string) {
    setError(null);
    try {
      const [evidence, items] = await Promise.all([
        api.getCitation(claimId, fieldPath),
        api.listReviews({ scope: "pool", status: "open", type: "FIELD_VERIFY", claim_id: claimId }),
      ]);
      setCitation(evidence);
      setFieldReview(items.find((item) => item.payload.path === fieldPath) ?? null);
      if (api.getDocument) {
        const [pdfjs, worker, bytes] = await Promise.all([
          import("pdfjs-dist"),
          import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
          api.getDocument(evidence.document_url),
        ]);
        pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
        const document = await pdfjs.getDocument({ data: bytes }).promise;
        const page = await document.getPage(evidence.page);
        const base = page.getViewport({ scale: 1 });
        const view = page.getViewport({ scale: Math.min(1.5, 820 / base.width) });
        const canvas = citationCanvas.current;
        const context = canvas?.getContext("2d");
        if (canvas && context) {
          canvas.width = view.width;
          canvas.height = view.height;
          await page.render({ canvas, canvasContext: context, viewport: view }).promise;
          const rotation = ((view.rotation % 360) + 360) % 360 as 0 | 90 | 180 | 270;
          setCitationViewport({ width: view.width, height: view.height, rotation });
          canvas.scrollIntoView({ block: "nearest" });
        }
      }
    } catch (caught) {
      setError(consoleError(caught, "Citation evidence could not be opened."));
    }
  }

  if (error && !claim) return <main className="claim-page"><ReadError error={error} onRetry={() => setReload((value) => value + 1)} /></main>;
  if (!claim) return <main className="claim-page"><p className="loading-state">Loading claim…</p></main>;

  const amount = claim.header.amount_cents === null
    ? "Amount unavailable"
    : formatKes(parseCents(claim.header.amount_cents));
  const partyFields = claim.fields.filter((field) => field.path.startsWith("parties."));
  const keyFields = claim.fields.filter((field) => !field.path.startsWith("parties.")).slice(0, 8);
  const timeline = calcFocus === null
    ? claim.timeline
    : claim.timeline.filter((item) => formatStructured(item).includes(calcFocus));

  return (
    <main className="claim-page">
      {claim.claim.status === "DECLINED" && (
        <section role="alert" aria-label="Claim declined" className="decline-banner">
          <div><strong>Claim declined</strong><span>{claim.claim.substatus ?? "No substatus supplied"}</span></div>
          <button onClick={() => setReopenNotice(true)}>Reopen claim</button>
          {reopenNotice && <p>Reopen unavailable — PRD-05</p>}
        </section>
      )}
      <header className="claim-header">
        <div><p className="workspace-kicker">Claim</p><h1>{claim.claim.id}</h1></div>
        <dl>
          <div><dt>Insured</dt><dd>{displayValue(claim.header.insured)}</dd></div>
          <div><dt>Registration</dt><dd>{displayValue(claim.header.registration)}</dd></div>
          <div><dt>Routing amount</dt><dd>{amount}</dd></div>
          <div><dt>Updated</dt><dd>{formatEat(claim.claim.updated_at)}</dd></div>
        </dl>
      </header>
      <ol className="status-rail" aria-label="Claim status rail">
        {CLAIM_STATES.map((state) => (
          <li key={state} aria-label={`Claim state ${state}`} aria-current={state === claim.claim.status ? "step" : undefined}>
            <span>{state.replaceAll("_", " ")}</span>
          </li>
        ))}
      </ol>
      <div role="tablist" aria-label="Claim sections" className="claim-tabs">
        {TABS.map((tab) => (
          <button key={tab} role="tab" aria-selected={activeTab === tab} onClick={() => { setActiveTab(tab); if (tab !== "Timeline") setCalcFocus(null); }}>
            {tab}
          </button>
        ))}
      </div>
      <section role="tabpanel" className="claim-tab-panel">
        {activeTab === "Overview" && (
          <div className="overview-grid">
            <section>
              <p className="workspace-kicker">Parties</p>
              <h2>Claim participants</h2>
              {partyFields.length === 0 ? <p className="empty-state">No committed party fields.</p> : partyFields.map((field) => (
                <article key={field.path} className="field-card">
                  <span>{humanLabel(field.path)}</span>
                  <strong>{fieldValue(field.value, field.value_type)}</strong>
                  <small>{humanLabel(field.verification_state)}</small>
                </article>
              ))}
            </section>
            <section>
              <p className="workspace-kicker">Key fields</p>
              <h2>Decision inputs</h2>
              {keyFields.length === 0 ? <p className="empty-state">No committed key fields.</p> : keyFields.map((field) => (
                <article key={field.path} className="field-card">
                  <span>{humanLabel(field.path)}</span>
                  <strong>{fieldValue(field.value, field.value_type)}</strong>
                  <div className="badge-row">
                    <span className="state-badge">{humanLabel(field.verification_state)}</span>
                    <span className="confidence-badge">
                      {field.confidence === null ? "Confidence unavailable" : `${Math.round(field.confidence * 100)}% confidence`}
                    </span>
                  </div>
                </article>
              ))}
            </section>
          </div>
        )}
        {activeTab === "Documents" && (
          <div>
            <p className="workspace-kicker">Evidence register</p><h2>Documents</h2>
            {claim.availability.document_checklist?.status === "not_available" && (
              <p className="availability-state">Checklist not available until {claim.availability.document_checklist.owner} is installed.</p>
            )}
            {claim.documents.length === 0 ? <p className="empty-state">No committed document records.</p> : (
              <div className="table-scroll"><table><thead><tr><th>File</th><th>Type</th><th>Status</th><th>Pages</th><th>Received</th></tr></thead><tbody>
                {claim.documents.map((document, index) => {
                  const row = record(document);
                  return <tr key={String(row.id ?? index)}><td>{displayValue(row.filename)}</td><td>{displayValue(row.doc_type)}</td><td>{displayValue(row.status)}</td><td>{displayValue(row.page_count)}</td><td>{typeof row.received_at === "string" ? formatEat(row.received_at) : "Unavailable"}</td></tr>;
                })}
              </tbody></table></div>
            )}
            {/* PRD-08 §8.2: the approval-pack readiness card lives on Claim 360
                inside the evidence tab. PRD-04 §4.3 fixes the seven tab names,
                so no eighth tab is invented. */}
            <ApprovalPackReadiness
              api={api}
              claimId={claimId}
              documents={claim.documents}
              communications={claim.communications}
            />
          </div>
        )}
        {activeTab === "Fields & Citations" && (
          <div className="fields-workspace">
            <div className="table-scroll"><table>
              <thead><tr><th>Field</th><th>Value</th><th>Type</th><th>Verification</th><th>Confidence</th><th>Evidence</th></tr></thead>
              <tbody>{claim.fields.map((field) => (
                <tr key={field.path}><td>{humanLabel(field.path)}</td><td>{fieldValue(field.value, field.value_type)}</td><td>{field.value_type}</td><td>{humanLabel(field.verification_state)}</td><td>{field.confidence === null ? "—" : `${Math.round(field.confidence * 100)}%`}</td><td><button disabled={!field.has_citation} onClick={() => void openCitation(field.path)}>{field.has_citation ? "Open citation" : "No citation"}</button></td></tr>
              ))}</tbody>
            </table></div>
            {citation && (
              <section className="citation-viewer" aria-label="Citation viewer">
                <div className="citation-page">
                  <canvas ref={citationCanvas} aria-label={`Document page ${citation.page}`} />
                  {citationViewport && <CitationOverlay bbox={citation.bbox} viewport={citationViewport} label={`Cited value on page ${citation.page}`} />}
                </div>
                <div className="citation-value"><span>Current value</span><strong>{fieldValue(citation.value, citation.value_type)}</strong><small>Page {citation.page} · exact stored bounding box</small></div>
              </section>
            )}
            {fieldReview && <Workspace item={fieldReview} api={api} />}
            {citation && !fieldReview && <p className="availability-state">Verification review is not available for this field.</p>}
          </div>
        )}
        {activeTab === "Financials" && (
          <div><p className="workspace-kicker">Committed money fields</p><h2>Financials</h2>
            {claim.financials.length === 0 ? <p className="empty-state">No committed financial fields.</p> : (
              <div className="table-scroll"><table><thead><tr><th>Figure</th><th>Amount</th><th>Calculation lineage</th></tr></thead><tbody>
                {claim.financials.map((row) => <tr key={row.path}><td>{humanLabel(row.path)}</td><td>{formatKes(parseCents(row.amount_cents))}</td><td>{row.calc_run_id ? <button className="link-button" onClick={() => { setCalcFocus(row.calc_run_id); setActiveTab("Timeline"); }}>Calc run {row.calc_run_id}</button> : <span className="availability-state">No calc_run_id supplied</span>}</td></tr>)}
              </tbody></table></div>
            )}
          </div>
        )}
        {activeTab === "Timeline" && (
          <div><p className="workspace-kicker">Event spine</p><h2>{calcFocus ? `Events for calculation ${calcFocus}` : "Timeline"}</h2>
            {calcFocus && <button className="link-button" onClick={() => setCalcFocus(null)}>Show all events</button>}
            {timeline.length === 0 ? <p className="empty-state">{calcFocus ? "No committed event references this calculation run." : "No committed timeline events."}</p> : (
              <ol className="event-list">{timeline.map((item, index) => { const row = record(item); const eventType = String(row.type ?? row.event_type ?? "event"); return <li key={String(row.id ?? index)}><header><strong>{humanLabel(eventType)}</strong><time>{typeof row.occurred_at === "string" ? formatEat(row.occurred_at) : "Time unavailable"}</time></header><Facts value={row} omit={["id", "type", "event_type", "occurred_at"]} /></li>; })}</ol>
            )}
          </div>
        )}
        {activeTab === "Systems" && (
          <div><p className="workspace-kicker">External projection</p><h2>Systems</h2>
            {/* PRD-09 §9.3 / PACKET-20: the operation catalogue and paste strip
                replace the unavailable placeholder inside the existing Systems
                tab. PRD-04 §4.3 fixes the seven tab names, so no eighth tab is
                invented. */}
            <ProjectionSystems api={api} claimId={claimId} />
            {claim.systems.length > 0 && <div className="record-grid">{claim.systems.map((item, index) => { const row = record(item); return <article key={String(row.id ?? index)}><h3>{humanLabel(String(row.system ?? row.event_type ?? "system"))}</h3><Facts value={row} omit={["id", "system", "event_type"]} /></article>; })}</div>}
          </div>
        )}
        {activeTab === "Communications" && (
          <div><p className="workspace-kicker">Claim correspondence</p><h2>Communications</h2>
            {claim.availability.communications?.status === "not_available" ? <p className="availability-state">Not available until {claim.availability.communications.owner} is installed.</p> : claim.communications.length === 0 ? <p className="empty-state">No committed communication events.</p> : <ol className="thread-list">{claim.communications.map((item, index) => { const row = record(item); return <li key={String(row.id ?? index)}><h3>{displayValue(row.subject ?? row.event_type ?? "Communication")}</h3><Facts value={row} omit={["id", "subject", "event_type"]} /></li>; })}</ol>}
          </div>
        )}
      </section>
      {error && <ReadError error={error} onRetry={() => setError(null)} />}
    </main>
  );
}
