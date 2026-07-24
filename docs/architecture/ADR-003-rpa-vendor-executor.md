# ADR-003 — Vendor RPA executor strategy

- **Status:** accepted; vendor selection and production activation pending
- **Decision date:** 2026-07-24
- **Owners:** Pacha CTO and repository owner
- **Supersedes:** the planned custom Playwright runner, leasing and hosting

## Context

Pacha must project exact, governed data into ICON and EDMS while guaranteeing
readback and zero silent divergence. Building a generic browser-automation
runtime, session manager and runner fleet is commodity work with significant
operational risk.

## Decision

Paste-assist remains the safe production mode for the first pilot.

For purchased UI execution, evaluate Microsoft Power Automate Desktop first.
Evaluate UiPath only if Power Automate cannot meet Pacha's control, evidence,
identity, deployment and target-system requirements. No vendor is production
approved by this decision.

Pacha owns:

- the exact typed payload and target operation definition;
- verification floors and authority/autonomy decisions;
- the stable idempotency/write identifier;
- the only call through `execute_or_stage`;
- the evidence requirements and immutable evidence references;
- independent target-system readback and reconciliation; and
- `ui_drift`, divergence and `uncertain_write` handling.

The vendor executor may perform declared UI actions and return execution
evidence. It does not decide values, authority, retries or reconciliation.

AI selector repair and vendor self-healing are disabled. A selector miss,
unexpected screen or cardinality mismatch fails closed as
`EXCEPTION{ui_drift}` and the operation falls back to paste-assist. An uncertain
external write becomes `EXCEPTION{uncertain_write}` and is not automatically
retried.

Pacha will not build custom Playwright automation, runner leasing, generic
browser hosting, session management or target-specific selector hunting.

## Vendor acceptance

Activation requires an RFI/control assessment covering dedicated service
identity, least privilege, audit evidence, data location and sub-processors,
malware/credential handling, target reachability, selector-repair disablement,
support/SLA, cost, exit/export and DPIA/procurement approval.

## Consequences

The custom RPA work described by the earlier `PACKET-21`/`PACKET-22` hand-off is
frozen and must be reissued around a vendor-neutral executor contract.
Synthetic executor tests do not establish that a vendor or operation is live.
