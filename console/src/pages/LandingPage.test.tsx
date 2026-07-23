import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LandingPage } from "./LandingPage";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("LandingPage", () => {
  it("presents the supplied Pacha narrative and pilot actions", () => {
    render(<LandingPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Pacha" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /I spent weeks on the claims floor/i })).toBeInTheDocument();
    expect(screen.getByText("(90%)")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Run a claim through Pacha." })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Request a pilot" })).toHaveLength(2);
    expect(screen.queryByText("(First claim free)")).not.toBeInTheDocument();
  });

  it("links the header to the pilot section and exposes a content skip link", () => {
    render(<LandingPage />);

    expect(screen.getByRole("link", { name: "(Request a pilot)" })).toHaveAttribute("href", "#pilot");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#claims-floor");
    expect(screen.getByRole("link", { name: "hello@pacha.co.ke" })).toHaveAttribute("href", "mailto:hello@pacha.co.ke");
  });
});
