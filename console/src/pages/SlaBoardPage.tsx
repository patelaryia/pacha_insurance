import React, { useEffect, useState } from "react";

import type { ConsoleApi, SlaClockRow } from "../api/types";

interface SlaBoardPageProps { api: ConsoleApi; }

export function SlaBoardPage({ api }: SlaBoardPageProps) {
  const [clocks, setClocks] = useState<SlaClockRow[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [blocked, setBlocked] = useState<Set<string>>(new Set());
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let active = true;
    const getSlaBoard = api.getSlaBoard;
    if (!getSlaBoard) return () => { active = false; };
    getSlaBoard.call(api).then((result) => { if (active) setClocks(result.clocks); });
    return () => { active = false; };
  }, [api]);

  function toggle(clockId: string) {
    setSelected((current) => current.includes(clockId)
      ? current.filter((id) => id !== clockId)
      : [...current, clockId]);
  }

  async function escalate() {
    if (selected.length === 0) return;
    if (!api.escalateClocks) return;
    setPending(true);
    try {
      const response = await api.escalateClocks(selected);
      setBlocked(new Set(
        response.results
          .filter((row) => row.outcome === "blocked_on_inputs")
          .map((row) => row.clock_id),
      ));
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="ops-page">
      <header className="ops-header">
        <p className="workspace-kicker">S-5 · Breach proximity</p>
        <h1>SLA board</h1>
        <button
          data-testid="sla-escalate"
          disabled={pending || selected.length === 0}
          onClick={() => void escalate()}
        >
          Bulk escalate selected
        </button>
      </header>
      <section className="ops-panel table-scroll">
        <table>
          <thead><tr><th>Select</th><th>Claim</th><th>Clock</th><th>Breach</th><th>Escalation</th></tr></thead>
          <tbody>
            {clocks.map((clock) => (
              <tr key={clock.clock_id} data-testid={`sla-row-${clock.clock_id}`}>
                <td><input aria-label={`Select ${clock.clock_id}`} type="checkbox" checked={selected.includes(clock.clock_id)} onChange={() => toggle(clock.clock_id)} /></td>
                <td>{clock.claim_id}</td>
                <td>{clock.definition_id}<br /><small>{clock.state}</small></td>
                <td>{clock.breach_at ?? "No deadline"}</td>
                <td>
                  {blocked.has(clock.clock_id) ? (
                    <strong data-testid={`sla-blocked-${clock.clock_id}`}>blocked_on_inputs</strong>
                  ) : clock.escalate_to_role}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
