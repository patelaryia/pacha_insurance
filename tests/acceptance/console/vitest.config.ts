import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const consoleRoot = path.resolve(here, "../../../console");
const consoleRequire = createRequire(path.join(consoleRoot, "package.json"));

function fromConsole(specifier: string): string {
  return consoleRequire.resolve(specifier);
}

export default async function protectedAcceptanceConfig() {
  const react = (
    await import(pathToFileURL(fromConsole("@vitejs/plugin-react")).href)
  ).default;
  return {
    root: consoleRoot,
    plugins: [react()],
    server: {
      fs: { allow: [consoleRoot, path.resolve(consoleRoot, "..")] },
    },
    resolve: {
      alias: [
        {
          find: "@testing-library/jest-dom/vitest",
          replacement: path.resolve(
            consoleRoot,
            "node_modules/@testing-library/jest-dom/dist/vitest.mjs",
          ),
        },
        {
          find: "@testing-library/react",
          replacement: path.resolve(
            consoleRoot,
            "node_modules/@testing-library/react/dist/@testing-library/react.esm.js",
          ),
        },
        {
          find: "@testing-library/user-event",
          replacement: path.resolve(
            consoleRoot,
            "node_modules/@testing-library/user-event/dist/esm/index.js",
          ),
        },
        { find: "axe-core", replacement: fromConsole("axe-core") },
        {
          find: "react/jsx-dev-runtime",
          replacement: fromConsole("react/jsx-dev-runtime"),
        },
        {
          find: "react/jsx-runtime",
          replacement: fromConsole("react/jsx-runtime"),
        },
        { find: "react", replacement: fromConsole("react") },
      ],
    },
    test: {
      environment: "jsdom",
      include: [
        path.resolve(here, "test_packet_11_console.test.tsx"),
        path.resolve(here, "test_packet_12_console.test.tsx"),
        path.resolve(here, "test_packet_19_console.test.tsx"),
        path.resolve(here, "test_packet_20_console.test.tsx"),
        path.resolve(here, "test_packet_21_console.test.tsx"),
        path.resolve(consoleRoot, "src/**/*.test.{ts,tsx}"),
      ],
      coverage: {
        provider: "v8",
        all: true,
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/main.tsx", "src/**/*.d.ts", "src/**/*.test.{ts,tsx}"],
        thresholds: {
          statements: 70,
          branches: 70,
          functions: 70,
          lines: 70,
        },
      },
    },
  };
}
