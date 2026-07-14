import React, { useCallback, useEffect, useRef, useState } from "react";

import {
  REVIEW_TYPES,
  type ConsoleApi,
  type ReviewItem,
  type ReviewScope,
  type ReviewType,
} from "../api/types";
import { consoleError, type ConsoleError } from "../api/errors";
import { Workspace } from "../workspaces/registry";

interface ReviewQueuePageProps { api: ConsoleApi; }

function isEditingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.matches("input, textarea, select, [contenteditable='true']");
}

export function ReviewQueuePage({ api }: ReviewQueuePageProps) {
  const [scope, setScope] = useState<ReviewScope>("mine");
  const [type, setType] = useState<ReviewType | "">("");
  const [status, setStatus] = useState("");
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<ConsoleError | null>(null);
  const rowRefs = useRef<Array<HTMLDivElement | null>>([]);

  const load = useCallback(async () => {
    setLoading(true);
    setReadError(null);
    try {
      const next = await api.listReviews({
        scope,
        ...(type ? { type } : {}),
        ...(status ? { status } : {}),
      });
      setItems(next);
      setSelectedIndex((current) => Math.min(current, Math.max(next.length - 1, 0)));
    } catch (caught) {
      setReadError(consoleError(caught, "Queue unavailable. Retry the read."));
    } finally {
      setLoading(false);
    }
  }, [api, scope, status, type]);

  useEffect(() => { void load(); }, [load]);

  function onKeyDown(event: React.KeyboardEvent<HTMLElement>) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (isEditingTarget(event.target)) {
      if (event.key === "Escape") {
        event.preventDefault();
        rowRefs.current[selectedIndex]?.focus();
      }
      return;
    }
    const option = (event.target as HTMLElement).closest<HTMLElement>("[role='option']");
    if (!option || document.activeElement !== option) return;
    if (event.key === "j" || event.key === "k") {
      event.preventDefault();
      const direction = event.key === "j" ? 1 : -1;
      const next = Math.min(Math.max(selectedIndex + direction, 0), items.length - 1);
      setSelectedIndex(next);
      rowRefs.current[next]?.focus();
      return;
    }
    const action = ({ a: "approve", e: "edit_approve", r: "reject" } as const)[event.key as "a" | "e" | "r"];
    if (action) {
      event.preventDefault();
      event.currentTarget.querySelector<HTMLButtonElement>(`[data-action='${action}']`)?.click();
    }
  }

  const selected = items[selectedIndex];
  return (
    <main className="queue-page" onKeyDown={onKeyDown}>
      <section className="queue-pane" aria-label="Review queue">
        <header>
          <p>Claims operations</p>
          <h1>Review queue</h1>
        </header>
        <div role="listbox" aria-label="Review items" className="review-list">
          {loading && items.length === 0 && <p>Loading review items…</p>}
          {!loading && items.length === 0 && !readError && <p>No items match this view.</p>}
          {items.map((item, index) => (
            <div
              key={item.id}
              ref={(node) => { rowRefs.current[index] = node; }}
              role="option"
              aria-selected={index === selectedIndex}
              aria-label={`${item.type} ${item.claim_id ?? "unlinked"}`}
              tabIndex={index === selectedIndex ? 0 : -1}
              className="review-row"
              onFocus={() => setSelectedIndex(index)}
            >
              <span>{item.type}</span>
              <strong>{item.claim_id ?? "No claim"}</strong>
              <span>{item.assigned_to ?? "Unassigned"}</span>
              <span className="sla-chip">{item.sla[0]?.state?.toString() ?? "No SLA"}</span>
            </div>
          ))}
        </div>
        {(!loading || items.length > 0) && <div className="queue-filters">
          <div role="group" aria-label="Queue ownership">
            <button aria-pressed={scope === "mine"} onClick={() => setScope("mine")}>Mine</button>
            <button aria-pressed={scope === "pool"} onClick={() => setScope("pool")}>Pool</button>
          </div>
          <label>
            Item type
            <select value={type} onChange={(event) => setType(event.target.value as ReviewType | "")}>
              <option value="">All types</option>
              {REVIEW_TYPES.map((name) => (
                <option key={name} value={name}>
                  {name.toLowerCase().replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label>
            Status
            <select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">All statuses</option>
              <option value="open">Open</option>
              <option value="resolved">Resolved</option>
            </select>
          </label>
        </div>}
        {readError && (
          <section role="alert" className={`read-state read-state-${readError.kind}`}>
            <p className="workspace-kicker">
              {readError.kind === "authentication"
                ? "Authentication required"
                : readError.kind === "authorisation"
                  ? "Access denied"
                  : "Read interrupted"}
            </p>
            <strong>{readError.code}</strong>
            <p>{readError.detail}</p>
            <p>
              {readError.kind === "authentication"
                ? "Refresh your Microsoft sign-in, then retry."
                : readError.kind === "authorisation"
                  ? "Ask an administrator to verify your immutable identity mapping and claims role."
                  : "The selected item remains unchanged."}
            </p>
            {readError.kind !== "authorisation" && <button onClick={() => void load()}>Retry</button>}
          </section>
        )}
      </section>
      <section className="workspace-pane" aria-live="polite">
        {selected ? (
          <Workspace key={selected.id} item={selected} api={api} onResolved={load} />
        ) : (
          <p>Select a review item.</p>
        )}
      </section>
    </main>
  );
}
