---
id: PACKET-21
prd_ref: docs/AGENT_BUILD_GUIDE.md
title: Frozen; reissue required
depends_on:
- PACKET-20
status: blocked
branch: codex/packet-21-frozen-reissue-required
attempts: 0
blast_radius: true
acceptance_tests: []
review_findings: []
escalation_reason: Frozen 2026-07-24 before issue under ADR-003. The custom Playwright runner
  scope is superseded and must not be built. Not sliceable until the vendor RFI/control assessment
  decides the executor. No acceptance tests exist because there is no agreed contract to test.
---

# PACKET-21 — Frozen; reissue required

**Status:** frozen before issue or implementation; **not complete**
**Freeze date:** 2026-07-24
**Authority:** ADR-003 and PRD-09 v1.1 architecture freeze

The former PACKET-21 scope was the custom Playwright runner plus the
zero-silent-divergence control plane. The runner, runner leasing/hosting,
session management and generic browser-automation portions are superseded and
must not be built.

Reissue PACKET-21 only after the vendor RFI/control assessment. Its replacement
scope is:

- evaluate Microsoft Power Automate Desktop first and UiPath only if Power
  Automate cannot meet the recorded controls;
- define the vendor-neutral executor contract and operation-specific
  acceptance evidence;
- preserve Pacha's stable idempotency, `execute_or_stage`, readback,
  reconciliation, evidence, circuit and fail-closed `ui_drift` controls; and
- keep paste-assist as the accepted fallback.

The reissue requires security/DPIA, procurement, target reachability, service
identity and owner approval. This notice is not an implementation packet and
cannot be used to claim live-operation acceptance.
