import React from "react";
import ReactDOM from "react-dom/client";
import { LandingPage } from "./pages/LandingPage";
import "./styles.css";

const root = document.getElementById("root")!;

if (window.location.pathname === "/") {
  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      <LandingPage />
    </React.StrictMode>,
  );
} else {
  void import("./ConsoleApp").then(({ bootstrapConsole }) => bootstrapConsole(root));
}
