import React, { useEffect, useState } from "react";

import type {
  AdapterHealthRow,
  CapabilityRow,
  ConsoleApi,
  LedgerRow,
  PackRow,
} from "../api/types";

interface AdminPageProps { api: ConsoleApi; }

export function AdminPage({ api }: AdminPageProps) {
  const [packs, setPacks] = useState<PackRow[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityRow[]>([]);
  const [ledgerAction, setLedgerAction] = useState("");
  const [ledger, setLedger] = useState<LedgerRow[]>([]);
  const [adapters, setAdapters] = useState<AdapterHealthRow[] | null>(null);
  const [circuitError, setCircuitError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    if (!api.getPacks || !api.getCapabilities) return () => { active = false; };
    void Promise.all([api.getPacks(), api.getCapabilities()]).then(([packRows, capabilityRows]) => {
      if (!active) return;
      setPacks(packRows.packs);
      setCapabilities(capabilityRows.capabilities);
      // The PACKET-12 placeholder shape is still honoured: a console talking to
      // an application without PRD-09 keeps its explicit unavailable card.
      setAdapters(Array.isArray(packRows.adapter_health) ? packRows.adapter_health : null);
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
        {adapters === null ? (
          <article
            className="ops-panel availability-state"
            data-testid="adapter-health-unavailable"
          >
            <h2>Adapter health</h2>
            <p>Unavailable until PRD-09 supplies the adapter registry.</p>
          </article>
        ) : (
          <article className="ops-panel" data-testid="adapter-health">
            <h2>Adapter health</h2>
            {circuitError && <p role="alert">{circuitError}</p>}
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>System</th><th>Configured</th><th>Effective</th>
                    <th>Status</th><th>Reason</th><th>Runner last seen</th><th>Circuits</th>
                  </tr>
                </thead>
                <tbody>
                  {adapters.map((row) => (
                    <tr key={row.system} data-testid={`adapter-row-${row.system}`}>
                      <td>{row.system.toUpperCase()}</td>
                      <td>{row.configured_mode.replaceAll("_", " ")}</td>
                      <td>{row.effective_mode.replaceAll("_", " ")}</td>
                      <td>{row.status.replaceAll("_", " ")}</td>
                      <td>{row.reason_code?.replaceAll("_", " ") ?? "None"}</td>
                      <td>{row.runner_last_seen_at ?? "Never"}</td>
                      <td>
                        {row.circuit_operation_ids.length === 0
                          ? "None open"
                          : row.circuit_operation_ids.map((operation) => (
                            <button
                              key={operation}
                              data-testid={`clear-circuit-${operation}`}
                              disabled={!api.clearProjectionCircuit}
                              onClick={() => {
                                setCircuitError(null);
                                void api.clearProjectionCircuit!(operation).catch(() =>
                                  setCircuitError(
                                    `${operation} is not qualified for reset. Install a newer `
                                    + "definition version and re-run its selector suite.",
                                  ));
                              }}
                            >
                              Clear {operation}
                            </button>
                          ))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </article>
        )}
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
