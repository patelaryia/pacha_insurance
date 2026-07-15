import React, { useEffect, useState } from "react";

import type { CapabilityRow, ConsoleApi, LedgerRow, PackRow } from "../api/types";

interface AdminPageProps { api: ConsoleApi; }

export function AdminPage({ api }: AdminPageProps) {
  const [packs, setPacks] = useState<PackRow[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityRow[]>([]);
  const [ledgerAction, setLedgerAction] = useState("");
  const [ledger, setLedger] = useState<LedgerRow[]>([]);

  useEffect(() => {
    let active = true;
    if (!api.getPacks || !api.getCapabilities) return () => { active = false; };
    void Promise.all([api.getPacks(), api.getCapabilities()]).then(([packRows, capabilityRows]) => {
      if (!active) return;
      setPacks(packRows.packs);
      setCapabilities(capabilityRows.capabilities);
    });
    return () => { active = false; };
  }, [api]);

  async function search(event: React.FormEvent) {
    event.preventDefault();
    if (!api.searchLedger) return;
    const response = await api.searchLedger(ledgerAction ? { action: ledgerAction } : {});
    setLedger(response.rows);
  }

  return (
    <main className="ops-page">
      <header className="ops-header">
        <p className="workspace-kicker">S-6 · Governed configuration</p>
        <h1>Administration</h1>
      </header>
      <section className="admin-grid">
        <article className="ops-panel">
          <h2>Installed packs</h2>
          {packs.map((pack) => (
            <div key={`${pack.id}-${pack.version}`} data-testid="pack-version-row">
              <strong>{pack.version}</strong><span>{pack.entries.length} registry entries</span>
            </div>
          ))}
        </article>
        <article className="ops-panel">
          <h2>Capabilities</h2>
          {capabilities.map((capability) => (
            <div key={capability.id} data-testid={`capability-row-${capability.id}`}>
              <strong>{capability.id}</strong><span>{capability.current_level} / {capability.max_level}</span>
            </div>
          ))}
        </article>
        <article className="ops-panel availability-state" data-testid="adapter-health-unavailable">
          <h2>Adapter health</h2>
          <p>Unavailable until PRD-09 supplies the adapter registry.</p>
        </article>
        <article className="ops-panel availability-state" data-testid="user-role-readonly">
          <h2>User and role assignments</h2>
          <p>Read-only, config-managed organisation data.</p>
        </article>
      </section>
      <section className="ops-panel">
        <h2>Audit ledger</h2>
        <form onSubmit={(event) => void search(event)}>
          <label>
            Action
            <input data-testid="ledger-search-input" value={ledgerAction} onChange={(event) => setLedgerAction(event.target.value)} />
          </label>
          <button type="submit">Search ledger</button>
        </form>
        <div className="table-scroll">
          <table><thead><tr><th>Seq</th><th>Action</th><th>Actor</th><th>Row hash</th></tr></thead>
            <tbody>{ledger.map((row) => (
              <tr key={row.seq} data-testid={`ledger-row-${row.seq}`}>
                <td>{row.seq}</td><td>{row.action}</td><td>{row.actor}</td><td><code>{row.row_hash}</code></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
