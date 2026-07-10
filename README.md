# Pacha

Claims platform for Pacha (Mayfair motor TPA, Kenya). Modular monolith, single
repo (ED-1). This repository is spec-driven: `/docs` is the binding source of
truth, built in the order and under the invariants defined there.

## Layout (ED-1)

| Path | What |
|---|---|
| `platform/` | Core substrate — claim object, event spine, ledger, gate, notify (PRD-00…04). |
| `agents/` | Agent runtime and the intake→settlement agents (PRD-05…12). |
| `packs/` | LOB packs — rules, calcs, templates, click-paths as **data** (PRD-13). |
| `console/` | React + TS status console / review queue (PRD-04). |
| `infra/` | Terraform; AWS af-south-1 (ED-3). |
| `docs/` | The v1.1 spec pack — read `docs/AGENT_BUILD_GUIDE.md` first. |
| `tools/ci/` | Invariant guards run in CI. |

## Roles

- **Builder:** see [`AGENTS.md`](AGENTS.md).
- **Reviewer (CTO agent):** see [`CLAUDE.md`](CLAUDE.md).

## Local checks (CI runs the same)

```
ruff check .
python tools/ci/money_float_lint.py    # ED-8: no float in Money signatures
python tools/ci/banned_calls.py         # AR-2: side effects only via execute_or_stage
pytest -q
```

## Merge policy

`main` is protected: every change lands via PR, requires **green CI** and
**code-owner approval**. Protected paths (`tests/acceptance/`, `.github/`,
`tools/ci/`, Section-0 docs) require owner review — see `.github/CODEOWNERS`.
