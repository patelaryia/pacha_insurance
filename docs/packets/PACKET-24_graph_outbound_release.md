---
id: PACKET-24
prd_ref: docs/Section_0.5_Shared_Agent_Runtime_v1.1.md AR-3/AR-3a/AR-3b;
  register items 1, 130, 157, 180, 295
title: Graph outbound transport, throttling and durable release
depends_on: [PACKET-23]
branch: codex/packet-24-graph-outbound
blast_radius: true
acceptance_tests:
  - tests/acceptance/test_packet_13_agent_runtime.py
  - tests/acceptance/test_packet_15_chase_agent.py
  - tests/acceptance/test_packet_24_graph_outbound.py
status: queued
pr: null
attempts: 0
reason: null
---

# PACKET-24 — Graph outbound transport and release

## 1. What to build

Replace the `transport_pending_capture` executor with one Graph transport
called only by the existing governed communications service. Persist a release
row and stable write ID before execution. Enforce the platform-wide 30
messages/minute token bucket and AR-3a send windows with a durable
`release_due_at`; restart never duplicates a send.

Attachments at or below 3 MiB use the simple send path. Larger attachments use
Graph upload sessions with exact 4 MiB chunks and resume from a persisted
opaque session/checkpoint. Recipient addresses and bytes never enter events,
logs or Temporal history. After an acknowledged send, persist the communication
and `email.sent` in one transaction. An outcome lost after scheduling becomes
`EXCEPTION{uncertain_write}` unless a target probe proves non-execution.

The fake Graph client drives acceptance. Missing production credentials return
`blocked_on_inputs{graph_credentials}` and leave the release pending.

PACKET-24 extends the same `GraphIntegration` handle with
`outbound.release_due(now)` and installs its executor into
`CommunicationsService`; callers never invoke the transport directly. The test
client implements `send_message`, `create_upload_session`,
`upload_chunk` and `probe_write`.

## 2. Constraints

G-COMM, `execute_or_stage`, autonomy decisions, template verification,
claim-party recipients and attachment ownership remain unchanged. Notify's
staff-only exemption remains isolated.

## 3. Explicit non-goals

No direct `graph_client.send` outside the approved transport, no blind retry,
no background thread and no Temporal Schedule. T07 owns release cadence.

## 4. Acceptance

The tests prove throttling, send windows, chunking, idempotency, restart,
uncertain-write handling, `email.sent` truth and privacy.
