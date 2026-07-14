import React from "react";

interface CitationOverlayProps {
  bbox: readonly [number, number, number, number];
  viewport: { width: number; height: number; rotation: 0 | 90 | 180 | 270 };
  label: string;
}

export function CitationOverlay({ bbox, viewport, label }: CitationOverlayProps) {
  const [x0, y0, x1, y1] = bbox;
  let left = x0 * viewport.width;
  let top = y0 * viewport.height;
  let width = (x1 - x0) * viewport.width;
  let height = (y1 - y0) * viewport.height;
  if (viewport.rotation === 90) {
    left = (1 - y1) * viewport.width;
    top = x0 * viewport.height;
    width = (y1 - y0) * viewport.width;
    height = (x1 - x0) * viewport.height;
  } else if (viewport.rotation === 180) {
    left = (1 - x1) * viewport.width;
    top = (1 - y1) * viewport.height;
  } else if (viewport.rotation === 270) {
    left = y0 * viewport.width;
    top = (1 - x1) * viewport.height;
    width = (y1 - y0) * viewport.width;
    height = (x1 - x0) * viewport.height;
  }
  return (
    <span
      aria-label={label}
      className="citation-overlay"
      style={{ position: "absolute", left, top, width, height }}
    />
  );
}
