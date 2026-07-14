/**
 * PACKET-11 acceptance (vitest) — PRD-04 §4.3 citation viewer bbox geometry.
 *
 * Protected (CODEOWNERS): the builder may not modify this file. The pdf.js
 * page renderer is injected via the renderPage seam pinned in
 * docs/packets/PACKET-11_console_shell.md §1.5, so this spec verifies overlay
 * geometry deterministically in jsdom. Bbox space: [x0, y0, x1, y1],
 * top-left origin, rendered-page pixels at scale 1 (doc_intel source_ref).
 * The synthetic stand-in for acceptance scenario 5 (corpus sampling is a
 * live-ops gate, proposed register #103).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";

import { CitationViewer } from "@console/components/CitationViewer";

const BLOB_URL = "/api/console/documents/01HDOC00000000000000000AAA/blob";
const CITATION = { page: 2, bbox: [100, 150, 300, 180] as [number, number, number, number] };

function fakeRenderPage() {
  return vi.fn(async (_blobUrl: string, _page: number, scale: number) => ({
    width: 600 * scale,
    height: 800 * scale,
  }));
}

function px(value: string | null): number {
  expect(value).toBeTruthy();
  return Number.parseFloat(value as string);
}

describe("citation viewer bbox highlight (PRD-04 §4.3)", () => {
  it("requests the cited page through the injected renderer", async () => {
    const renderPage = fakeRenderPage();
    render(
      <CitationViewer
        blobUrl={BLOB_URL}
        citation={CITATION}
        scale={1}
        renderPage={renderPage as never}
      />,
    );
    await waitFor(() => expect(renderPage).toHaveBeenCalled());
    const [blobUrl, page] = renderPage.mock.calls[0];
    expect(blobUrl).toBe(BLOB_URL);
    expect(page).toBe(2);
  });

  it("positions the highlight overlay exactly on the bbox at scale 1", async () => {
    render(
      <CitationViewer
        blobUrl={BLOB_URL}
        citation={CITATION}
        scale={1}
        renderPage={fakeRenderPage() as never}
      />,
    );
    const highlight = await screen.findByTestId("citation-highlight");
    const style = (highlight as HTMLElement).style;
    expect(px(style.left)).toBeCloseTo(100, 1);
    expect(px(style.top)).toBeCloseTo(150, 1);
    expect(px(style.width)).toBeCloseTo(200, 1);
    expect(px(style.height)).toBeCloseTo(30, 1);
  });

  it("scales the overlay with the render scale", async () => {
    render(
      <CitationViewer
        blobUrl={BLOB_URL}
        citation={CITATION}
        scale={1.5}
        renderPage={fakeRenderPage() as never}
      />,
    );
    const highlight = await screen.findByTestId("citation-highlight");
    const style = (highlight as HTMLElement).style;
    expect(px(style.left)).toBeCloseTo(150, 1);
    expect(px(style.top)).toBeCloseTo(225, 1);
    expect(px(style.width)).toBeCloseTo(300, 1);
    expect(px(style.height)).toBeCloseTo(45, 1);
  });
});
