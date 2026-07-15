import React, { useEffect, useState } from "react";

import type { ConsoleApi, ReviewItem } from "../api/types";
import { Workspace } from "../workspaces/registry";

interface ApprovalsPageProps { api: ConsoleApi; }

export function ApprovalsPage({ api }: ApprovalsPageProps) {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.listReviews({ scope: "band", status: "open" })
      .then((result) => {
        if (!active) return;
        const compatible = result as ReviewItem[] | { items: ReviewItem[] };
        setItems(Array.isArray(compatible) ? compatible : compatible.items);
      })
      .catch(() => { if (active) setError("Approval queue unavailable"); });
    return () => { active = false; };
  }, [api]);

  const item = items[selected];
  return (
    <main className="ops-page approval-page">
      <header className="ops-header">
        <p className="workspace-kicker">S-3 · Authority band</p>
        <h1>Approval workspace</h1>
      </header>
      <section className="approval-layout">
        <aside data-testid="approval-queue" className="ops-panel">
          <h2>Band queue</h2>
          {error && <p role="alert">{error}</p>}
          {!error && items.length === 0 && <p>No open approvals in your band.</p>}
          {items.map((row, index) => (
            <button
              key={row.id}
              aria-pressed={index === selected}
              onClick={() => setSelected(index)}
            >
              <strong>{row.type}</strong>
              <span>{row.claim_id ?? "Unlinked"}</span>
            </button>
          ))}
        </aside>
        <section className="ops-panel approval-detail">
          <div data-testid="approval-pack-unavailable" className="availability-state">
            <strong>Approval pack unavailable</strong>
            <p>Not available until PRD-08 is installed.</p>
          </div>
          <div className="availability-state">
            <strong>T-03 alert · pending_capture</strong>
            <p>The alert body remains blocked on the pack template capture.</p>
          </div>
          {item ? (
            <Workspace item={item} api={api} onResolved={() => undefined} />
          ) : (
            <p>Select an approval item.</p>
          )}
        </section>
      </section>
    </main>
  );
}
