import React, { useEffect, useState } from "react";

import type { ConsoleApi, PortfolioTile } from "../api/types";

interface PortfolioPageProps { api: ConsoleApi; }

function readable(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    typeof item === "bigint" ? item.toString() : item, 2) ?? "No committed rows";
}

export function PortfolioPage({ api }: PortfolioPageProps) {
  const [tiles, setTiles] = useState<PortfolioTile[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const getPortfolio = api.getPortfolio;
    if (!getPortfolio) {
      setError("Portfolio unavailable");
      return () => { active = false; };
    }
    getPortfolio.call(api)
      .then((result) => { if (active) setTiles(result.tiles); })
      .catch(() => { if (active) setError("Portfolio unavailable"); });
    return () => { active = false; };
  }, [api]);

  return (
    <main className="ops-page">
      <header className="ops-header">
        <p className="workspace-kicker">S-4 · Outcome evidence</p>
        <h1>Portfolio dashboard</h1>
      </header>
      {error && <p role="alert" className="read-state">{error}</p>}
      <section className="tile-grid">
        {tiles.map((tile) => (
          <article key={tile.series_id} data-testid={`tile-${tile.series_id}`} className="ops-panel">
            <p className="workspace-kicker">{tile.status}</p>
            <h2>{tile.series_id.replaceAll("_", " ")}</h2>
            {tile.status === "live" ? (
              <>
                <pre>{readable(tile.data)}</pre>
                <a
                  data-testid={`export-${tile.series_id}`}
                  href={api.seriesCsvUrl?.(tile.series_id) ?? "#"}
                >
                  Export CSV
                </a>
              </>
            ) : (
              <p>{tile.status}: window and denominator definitions are not captured.</p>
            )}
          </article>
        ))}
      </section>
    </main>
  );
}
