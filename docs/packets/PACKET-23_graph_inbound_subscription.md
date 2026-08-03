---
id: PACKET-23
prd_ref: docs/Section_0.5_Shared_Agent_Runtime_v1.1.md AR-3b;
  docs/PRD-05_Intake_and_Triage_Agent_v1.1.md §5.2; register items 1, 120, 127, 295
title: Graph inbound delta and subscription substrate
depends_on: [TEMPORAL-T06]
branch: codex/packet-23-graph-inbound
blast_radius: true
acceptance_tests:
  - tests/acceptance/test_packet_13_agent_runtime.py
  - tests/acceptance/test_packet_23_graph_inbound.py
status: queued
pr: null
attempts: 0
reason: null
---

# PACKET-23 — Graph inbound delta and subscription substrate

## 1. What to build

Create public package `graph_integration` with an injected `GraphClient`
protocol and idempotent `delta_once()` / `renew_once()` services. Add binding
storage for mailbox delta token, subscription id/expiry, hashed client-state
verification material and last successful poll. Commit the new delta token
only in the same transaction that durably records every normalised
`email.received` event in that page.

The 60-second delta query is authoritative. Webhooks validate the exact random
client state and only request an early poll; they never ingest message facts.
Redelivery is a no-op through `communications.graph_message_id`. Messages over
25 MiB create `EXCEPTION{inbound_message_too_large}` without storing content.
Attachment bytes enter the existing claim-core blob/document boundary and are
never logged.

Production configuration accepts tenant, client, shared-mailbox and secret
references only. Missing values make `delta_once`/`renew_once` return the
visible `blocked_on_inputs{graph_credentials}` disposition; no fake token,
mailbox or successful no-op is permitted. Tests inject a fake client.

The public constructor is
`build_graph_integration(app, client=None, config=None) -> GraphIntegration`.
The handle exposes `inbound.delta_once()`, `inbound.renew_once()` and
`inbound.accept_webhook(client_state)`. A test client implements
`delta_page(token)`, `renew_subscription(client_state)` and
`download_attachment(message_id, attachment_id)`.

## 2. Constraints

Reuse the PRD-05 router unchanged after emitting its exact durable event.
Secrets remain outside PostgreSQL/events. No Temporal dependency enters this
package; T07 wraps these domain methods.

## 3. Explicit non-goals

No outbound send, archive label, release queue, Temporal Schedule or live
credential. PACKET-24 owns outbound transport and throttling.

## 4. Acceptance

The tests prove page/token crash atomicity, redelivery, client-state refusal,
25 MiB handling, normalised event shape, secret-free records and blocked
missing credentials.
