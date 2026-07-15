# PACKET-12 — Notify module, S-3–S-6 operational surfaces & SLA board (PRD-04 slice 3 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-04_Status_Console_and_Review_Queue_v1.1.md`
> §4.2, §4.3 S-3/S-4/S-5/S-6, §4.4 notifications, §4.5 scenario 2;
> Section 0.5 AR-5 (and AR-2 whitelist); Section 0 ED-2/ED-7/ED-8/ED-11;
> PRD-03 §3.4/§3.6; PRD-00 SLA semantics; registers #13/#69/#78/#79/#86/
> #99–#107.
> Precedence: Section 0 → Section 0.5 → PRD-04/PRD-03/PRD-00 → PACKET-10/11
> contracts → this packet.
> **Depends on:** PACKET-11 merged on main (`dae7f45`), registers #99–#107.
> **Acceptance:** `tests/acceptance/test_packet_12_notify_ops.py` and
> `tests/acceptance/console/test_packet_12_console.test.tsx` — protected,
> failing by design until this packet is built.
> **Packet 13 (next):** Phase-2 kickoff — AR-2 `execute_or_stage` gate module
> (register #68) + PRD-05 intake/triage slice 1.

## 0. Slice position

Closes PRD-04. PACKET-10 made review items durable; PACKET-11 gave S-1/S-2 and
trusted identity. This slice adds the AR-5 `notify/` module (websocket live,
email staged until open item 1), the daily digest, S-3 Approval Workspace,
S-4 Portfolio Dashboard, S-5 SLA Board with bulk escalate, and S-6 Admin.
The PACKET-10 resolution engine and PACKET-11 identity boundary are consumed
unchanged.

## 1. Scope

**In:**

1. **`platform/notify/` package (AR-5), import `notify`.** The *only*
   whitelisted direct-send path (`tools/ci/banned_calls.py` exempts `notify/`
   by directory part — the Graph transport must live inside this package).
   Staff notifications only; exempt from G-COMM and the autonomy gate; every
   send still ledgered. Components:
   - **Rules as pack data** — `packs/motor/notify/notify.yaml`. Immediate
     rules exactly per §4.4: `sla.breached`, `projection.diverged`,
     `grader.failed` **critical only** (payload `severity` filter),
     `autonomy.demoted`. Each rule: `event_type`, optional `when` payload
     filter, `audience`, `channels` (`in_app`, `email`), `template` ref
     (uncaptured template bodies ship `pending_capture`; a pending template
     never blocks the in-app row — the payload renders structurally).
     Audience values: `assigned_officer` (the claim's `claims.assigned_to`)
     or `role:<role>` (every actor with that role in the org roles mapping).
     Audience assignments are provisional CTO values needing capture
     confirmation (proposed #111).
   - **`notifications` table** (new, on `claim_core` Base, migration
     included — local DDL, proposed #110): `id` ULID PK; `recipient` text
     (`user:<ULID>`); `rule_id`; `event_id` (FK-free text; unique together
     with `recipient`+`channel` for idempotent projection); `claim_id`
     nullable; `channel` `'in_app'|'email'`; `status`
     `'sent'|'staged'|'read'`; `payload` JSON; `created_at`; `read_at`
     nullable. A projection of the event spine — rebuildable, append-only
     apart from the `read` status flip.
   - **Consumer on the existing dispatcher**: matching events → one row per
     recipient×channel, idempotent, synchronously drivable with
     `dispatch_once()`. `in_app` rows commit as `sent` and are pushed to
     connected websockets. `email` rows: the Graph transport requires the
     shared mailbox / `svc-pacha` accounts (**open item 1 — uncaptured**),
     so the committed transport config is `pending_capture` and email rows
     commit as `staged` with a visible `blocked_on: "open-item-1"` marker in
     `payload` — never silently dropped, never fake-sent (proposed #108).
     When credentials land, only transport config changes.
   - **Ledger**: `notify.sent` and `notify.staged` join `ledger.ACTION_MAP`
     via the single-writer consumer (authorised `claim_core` change,
     proposed #109); no direct `audit_ledger` writes.
   - **Websocket** `WS /console/ops/ws`: first message `{"token": ...}`
     verified with the same installed `TokenVerifier`; success →
     `{"type": "ready", "actor": ...}`; failure → close code **4401**
     (header auth is impossible in-browser for websockets — middleware
     exemption pinned, proposed #118). Pushes
     `{"type": "notification", "notification_id", "event_type", "claim_id"}`
     to the mapped recipient's sockets.
   - **Daily digest**: `notify_handle.run_digest(now)` synchronous callable +
     Celery Beat entry `notify.daily_digest` at **08:00 EAT**
     (`Africa/Nairobi` crontab in `notify.yaml`). Per officer with owned
     claims: open review-item count, breached/warned clocks, owned-claim
     state summary — computed from committed rows only, never invented.
     Idempotent per (officer, digest date): rerun creates nothing
     (proposed #113). `in_app` digest row `sent`; `email` digest `staged`
     per #108.
   - `GET /console/ops/notifications?scope=mine` (own rows only) and
     `POST /console/ops/notifications/{id}/read` (own rows only — the one
     permitted client write besides escalate/promote below).
2. **S-5 SLA Board API + screen.**
   `GET /console/ops/sla-board`: all open clocks (`stopped_at IS NULL`)
   sorted by breach proximity — ascending `breach_at`, nulls last; each row
   at least `{clock_id, claim_id, definition_id, state, started_at,
   breach_at, escalate_to_role}` (role read from the persisted
   `sla_definitions` registry).
   `POST /console/ops/sla-board/escalate` `{"clock_ids": [...]}` → per-row
   results: definition has a real `escalate_to_role` → `escalated`
   (emits `sla.escalated` event — ledgered via ACTION_MAP addition #109 —
   and routes a notification to that role through `notify`);
   `escalate_to_role: pending_capture` → `blocked_on_inputs`, visibly, per
   row — never guessed, never skipped silently (proposed #112). No claim
   state mutates. View roles: the S-1/S-2 operational set (register #105);
   escalate roles: `asst_claims_manager|claims_manager|gm|md|chairman`.
3. **S-4 Portfolio Dashboard API + screen** (roles: CM+, i.e.
   `claims_manager|gm|md|chairman`, plus `head_of_claims` per §4.2, plus
   read-only `auditor`; officers 403).
   `GET /console/ops/portfolio` → tiles, each
   `{series_id, status: "live"|"pending_capture"|"unavailable", data}`:
   - **Live now** (point-in-time / calendar-defined, computed from committed
     rows): `open_claims_by_state`, `sla_breaches` (open breached clocks,
     click-through ids), `per_officer_queue_depth` (open review items by
     `assigned_to`), `aging_histogram` (open claims by age bucket),
     `savings_mtd_ytd` (C-05 executed `calc_runs`, integer-cents sums, MTD/
     YTD per EAT calendar; empty data returns zeros — #79 precedent).
   - **`pending_capture`** — windowed trends (`autonomy_rate_trend`,
     `no_touch_trend`, `median_handling_trend`): register #79's window/
     denominator definitions are **still uncaptured**; `packs/motor/
     dashboard.yaml` ships the config slots with `window: pending_capture`
     and the tiles render blocked until the CM captures them (proposed
     #114). Never a locally invented window.
   `GET /console/ops/portfolio/{series_id}.csv` — every **live** tile is a
   saved, exportable series (`text/csv`, header row; this is the
   outcome-pricing evidence pack, §4.3). Pending tile → 409
   `SERIES_BLOCKED_ON_INPUTS`.
4. **S-3 Approval Workspace** (managers). `GET /reviews` gains read-only
   `scope=band`: band-gated open items (`PACK_REVIEW`, `EX_GRATIA`,
   `DRAFT_RELEASE` queue view) whose band amount falls within the actor's
   authority-matrix band — resolution authorisation itself is untouched
   PACKET-10. Screen: band queue left; right pane = the PACKET-11 workspace
   bindings with Approve / Edit→Approve / Reject unchanged; the merged-pack
   PDF pane and drafted-note pane are **PRD-08 producers** → explicit
   `Not available until PRD-08 is installed` state; the `>4M` T-03 alert
   slot renders `pending_capture` (template T-03, open item 6) with the
   threshold as pack config `4_000_000_00`, never a literal in code
   (proposed #116).
5. **S-6 Admin** (role `admin`; `auditor` read-only; CM for capability
   sign-off per §4.2):
   - `GET /console/ops/packs` — installed pack id/version plus versioned
     rule/calc/template registry entries (data the cop_runtime registry
     already holds); diff rendering is client-side over versions.
   - `GET /console/ops/capabilities` — the PACKET-08 capability table +
     promotion evidence + sign-offs, role-gated read.
     `POST /console/ops/capabilities/{id}/promote` — thin console transport
     into the **unchanged** PACKET-08 promotion service (sign-off
     validation, two-person L3→L4 #69, L0→L1 `CRITERIA_NOT_MET` #78);
     roles `claims_manager|admin`. The console-verified actor now backs the
     #69 sign-off actor claim — note this closes half of #69's "console-
     backed role verification lands with PRD-04".
   - `GET /console/ops/ledger?actor=&action=&claim_id=&after_seq=&limit=` —
     audit-ledger search (roles `auditor|admin|claims_manager`), returning
     rows **with** `seq`/`row_hash`/`before_hash`/`after_hash` (hash-chain
     visible). Read-only; the single-writer invariant is untouched.
   - **Adapter health**: no adapter registry exists before PRD-09 →
     explicit `unavailable{owner: PRD-09}` marker (proposed #117).
   - **User/role management**: identities/roles are org config files under
     change control (#99). S-6 ships a **read-only** viewer (mapping counts,
     role assignments, config file provenance) with a visible
     `config-managed` marker; mutation UI is deferred to a dedicated auth
     packet (proposed #115). Never a write path to identity config from the
     browser in this packet.
6. **Scenario 2 mechanics (§4.5(2)).** A post-triage decline is visible from
   queue (the `EXCEPTION{decline_approval_required}` item), from Claim-360
   (DECLINED banner state after CM approval), and from the portfolio
   (`open_claims_by_state` DECLINED count) — asserted end-to-end in
   acceptance within one synchronous dispatch drain. The *5-second
   production latency* remains a live gate per #104's pattern (proposed
   #114 note).
7. **Console SPA additions**: routes `/approvals`, `/portfolio`,
   `/sla-board`, `/admin`; pages exported as `ApprovalsPage`,
   `PortfolioPage`, `SlaBoardPage`, `AdminPage`, each `{ api }`-prop
   components per the PACKET-11 pattern; `ConsoleApi` gains the ops methods
   pinned in §3.1. All PACKET-11 data rules stay binding (bigint cents,
   EAT, visible error/blocked states, 1366×768, British English).

**Out / visibly unavailable:** real Graph email delivery (open item 1);
notification template verbatims (open item 6 class); trend windows (#79 —
still capture-gated); adapter registry + health data (PRD-09);
merged-pack PDF + drafted note (PRD-08); T-03 body (item 6); HOC approval
band (open item 13 — HOC stays bandless config); identity/role mutation UI;
Playwright E2E (deferred with the live-gate protocol); machine-ingress auth
(infra, #100); any new review type, capability, adapter op or payment path.

## 2. Binding spec quotes (implement verbatim)

PRD-04 §4.4:

> "In-app (websocket) + email: immediate for `sla.breached`,
> `projection.diverged`, `grader.failed(critical)`, `autonomy.demoted`;
> daily 08:00 EAT digest per officer (**digest = owned claims**, per the
> assignment model); escalation emails follow `escalate_to_role`. All
> notification sends ledgered."

> "**Transport (v1.1):** all staff notifications go through the `notify`
> module (Section 0.5 AR-5) — direct Graph send permitted, recipients
> restricted to the allowlisted staff domain, **exempt from G-COMM and the
> autonomy gate**, still ledgered. The AR-2 CI grep whitelists `notify/` by
> path."

Section 0.5 AR-5:

> "A separate `notify/` module sends all staff notifications (PRD-04 §4.4):
> direct Graph send is permitted here, recipients restricted to an
> allowlisted staff domain, templates registered, **exempt from G-COMM and
> the autonomy gate**, every send still ledgered."

PRD-04 §4.3:

> "**S-4 Portfolio Dashboard** (CM+). ... Every tile query is a saved,
> exportable series (CSV) — this is the outcome-pricing evidence pack."

> "**S-5 SLA Board.** All open clocks sorted by breach proximity; bulk
> escalate."

> "**S-6 Admin.** Pack version viewer (rules/calcs/templates with diffs),
> capability table with promotion evidence + sign-off flow (two-person where
> policy requires), adapter health, user/role management, audit-ledger
> search."

PRD-04 §4.5:

> "(2) decline on a claim is visible from queue, 360, and portfolio within
> 5s of the event"

## 3. Deliverable

```text
platform/notify/
  __init__.py        # build_notify(app, *, roles=None, config=None) -> handle
  rules.py           # pack rule loading + audience resolution
  consumer.py        # dispatcher consumer -> notifications rows + ws push
  transports.py      # in_app, staged email; Graph transport slot (item 1)
  digest.py          # run_digest(now) + Beat entry notify.daily_digest
  ws.py              # /console/ops/ws first-message token auth
packs/motor/notify/notify.yaml
packs/motor/dashboard.yaml
platform/review_queue/
  ops_api.py         # /console/ops/* routes (S-3..S-6 reads, escalate, promote)
  ops_reads.py       # sla board, portfolio series, ledger search, packs
platform/claim_core/alembic/versions/000X_notifications.py
console/src/pages/{ApprovalsPage,PortfolioPage,SlaBoardPage,AdminPage}.tsx
console/src/api/…     # ConsoleApi ops methods
docs/runbooks/ (content flagged in PR; CTO owns protected docs)
```

**Authorised existing-package changes, exactly these:**

1. `ledger.ACTION_MAP` gains `notify.sent`, `notify.staged`, `sla.escalated`
   (#109; precedent #70/#94). Single-writer consumer unchanged.
2. `review_queue` review reads gain `scope=band` (read-only filter); the
   resolution engine, closed 17-type enum, roles/bands are untouched.
3. `claim_core` gains the `notifications` model + migration (#110) — the
   only DDL in this packet.
4. `install_console` middleware exempts exactly `WS /console/ops/ws`
   (first-message auth instead, #118); every other `/console/*` HTTP route
   keeps bearer enforcement.

Nothing else. `.github/`, `tools/ci/`, protected acceptance files untouched
by the builder. Frontend CI steps already run the protected vitest config —
the packet-12 spec file is added to its `include` by this cut.

### 3.1 Pinned public surface (acceptance relies on exactly this)

Python:

```python
from notify import build_notify
from review_queue import install_ops

handle = build_notify(app, roles=None, config=None)
#   roles: actor -> role mapping (None loads packs/motor/routing/roles.yaml)
#   config: None loads packs/motor/notify/notify.yaml
handle.run_digest(now)        # synchronous; idempotent per officer+date
install_ops(app)              # after build_review_queue; before/after
                              # install_console both legal

# GET  /console/ops/notifications?scope=mine     -> {"items": [...]}
# POST /console/ops/notifications/{id}/read      -> 200 (own rows only)
# GET  /console/ops/sla-board                    -> {"clocks": [...]} sorted
# POST /console/ops/sla-board/escalate {clock_ids} ->
#      {"results": [{clock_id, outcome: "escalated"|"blocked_on_inputs", ...}]}
# GET  /console/ops/portfolio                    -> {"tiles": [...]}
# GET  /console/ops/portfolio/{series_id}.csv    -> text/csv | 409 SERIES_BLOCKED_ON_INPUTS
# GET  /console/ops/ledger?actor=&action=&claim_id=&after_seq=&limit=
# GET  /console/ops/packs
# GET  /console/ops/capabilities
# POST /console/ops/capabilities/{id}/promote
# WS   /console/ops/ws   first msg {"token"} -> {"type":"ready","actor"} | close 4401
# 403 FORBIDDEN_ROLE / 401 per PACKET-11 codes; 409 SERIES_BLOCKED_ON_INPUTS
```

Frontend (protected spec imports exactly these, PACKET-11 conventions):

```ts
import { ApprovalsPage } from "../../../console/src/pages/ApprovalsPage";
import { PortfolioPage } from "../../../console/src/pages/PortfolioPage";
import { SlaBoardPage } from "../../../console/src/pages/SlaBoardPage";
import { AdminPage } from "../../../console/src/pages/AdminPage";
// each: { api: ConsoleApi }
```

`ConsoleApi` gains: `getSlaBoard()`, `escalateClocks(clockIds)`,
`getPortfolio()`, `seriesCsvUrl(seriesId)`, `searchLedger(params)`,
`getPacks()`, `getCapabilities()`, `promoteCapability(id, body)`,
`listNotifications()`, `markNotificationRead(id)`.

Pinned test ids: `approval-queue`, `approval-pack-unavailable` (contains
"PRD-08"), `tile-<series_id>`, `export-<series_id>` (live tiles only),
`sla-row-<clock_id>`, `sla-escalate`, `sla-blocked-<clock_id>`,
`pack-version-row`, `capability-row-<id>`, `adapter-health-unavailable`
(contains "PRD-09"), `user-role-readonly` (contains "config"),
`ledger-search-input`, `ledger-row-<seq>`.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends with the implementation PR (PACKET-10/11 pattern):

- **#108 — §4.4 requires email but the Graph mailbox/service accounts are
  open item 1.** Channel-pluggable transports; `in_app` live; `email` rows
  commit `staged` with a visible `blocked_on` marker and a `pending_capture`
  transport config. Credentials landing changes config only.
- **#109 — notification sends and escalations must be ledgered but
  ACTION_MAP lacks entries.** Add `notify.sent`/`notify.staged`/
  `sla.escalated`; the single-writer event→ledger consumer remains the only
  writer (precedent #70/#94).
- **#110 — no PRD supplies notification persistence DDL.** Minimal
  `notifications` projection table (rebuildable from events apart from
  read-state); later columns need a register entry (precedent #89).
- **#111 — §4.4 names events but not recipients.** Audiences are pack data:
  `sla.breached`/`projection.diverged` → `assigned_officer`;
  `grader.failed(critical)`/`autonomy.demoted` → `role:claims_manager`
  (CM owns autonomy per §4.2). Provisional CTO values — capture
  confirmation wanted from Mayfair ops.
- **#112 — "bulk escalate" has no defined semantics.** Narrowest: per-clock
  `sla.escalated` event + notification to the definition's
  `escalate_to_role`; `pending_capture` roles → per-row visible
  `blocked_on_inputs`; no claim/clock state mutation. A stronger escalation
  action needs its own capture.
- **#113 — digest content/idempotency unstated beyond "08:00 EAT, owned
  claims".** Digest = committed-row summary (open items, breached/warned
  clocks, state counts) per officer; idempotent per officer+EAT-date; Beat
  entry `notify.daily_digest` with `Africa/Nairobi` crontab in pack config
  (precedent #86).
- **#114 — register #79 windows are still uncaptured at the dashboard
  packet.** Point-in-time/all-time/calendar (MTD/YTD) tiles ship live;
  windowed trend tiles read `dashboard.yaml` slots shipped
  `pending_capture` and render blocked; scenario-2's 5s latency and tile
  p95s stay live gates (#104 pattern). #79 remains open until the CM
  captures window/denominator definitions.
- **#115 — S-6 "user/role management" has no schema or API in any PRD and
  identities are org config under change control (#99).** Read-only viewer
  with config provenance; mutation UI deferred to a dedicated auth packet.
  Never a browser write path to identity config in this packet.
- **#116 — S-3's merged PDF/drafted note are PRD-08 artifacts; T-03 is
  uncaptured (item 6).** Producer-owned unavailable panes; T-03 alert slot
  `pending_capture`; the >4M threshold is pack config `4_000_000_00`.
- **#117 — no adapter registry exists before PRD-09.** Adapter health
  renders `unavailable{owner: PRD-09}`; no invented health rows.
- **#118 — browsers cannot set Authorization on websockets.** First-message
  token auth with the installed verifier, close 4401 on failure; the
  middleware exemption is exactly `WS /console/ops/ws`.

## 5. Builder guardrails

- **AR-5 boundary** — every send lives in `platform/notify/`; recipients
  restricted to the staff domain allowlist in `notify.yaml`; notify is
  exempt from G-COMM/autonomy gate but **never** from the ledger. No send
  call outside `notify/` (AR-2 grep enforces).
- **Never guess** — uncaptured transport, template, escalation role, trend
  window, T-03, adapter data, HOC band: all visible
  `pending_capture`/`blocked_on_inputs`/`unavailable` states.
- **Single-writer ledger** — all new ledger actions via ACTION_MAP + the
  existing consumer; ledger search is read-only and exposes the hash chain.
- **Closed enums** — 17 review types untouched; no new event types beyond
  `sla.escalated` (+ the two notify ledger actions); no new capability,
  adapter op, or funds-transfer path; GP-1 posture unchanged.
- **Autonomy ceilings** — S-6 promote is transport only; PACKET-08
  validation (sign-offs, two-person L3→L4, L0→L1 fail-closed #78) decides.
  Resolving/notifying never mutates `capabilities`.
- **RBAC server-side** — S-4 CM+/HOC/auditor; S-5 escalate ACM+; S-6
  admin/auditor/CM as pinned; officers 403 on manager surfaces; `auditor`
  read-only everywhere (no escalate, no promote, no read-marking others'
  rows).
- **Money integer cents** — savings/series sums BIGINT cents; CSV emits
  cents (no float formatting server-side); frontend stays `bigint`.
- **Config over code** — rules, audiences, allowlist, digest schedule,
  windows, 4M threshold, series definitions: pack data, never literals.
- All PACKET-01–11 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- Both protected acceptance files pass unmodified; full pytest green on
  SQLite + PostgreSQL; frontend lint/build/vitest green with coverage ≥70%
  on all four axes across `console/src`.
- Scenario 2 mechanics asserted end-to-end (queue item → CM approval →
  DECLINED on 360 → portfolio count) in one drain; latency itself recorded
  as a live gate.
- ≥80% coverage on `platform/notify/` and changed `review_queue` modules;
  migration reviewed (single `notifications` table); OpenAPI regenerated;
  runbook content flagged in the PR (notify triage: staged email backlog,
  ws connect failures, digest reruns, escalation blocked rows).
- Grader coverage: no new OutputType (notifications are not agent outputs);
  `grader_map.yaml` unchanged — confirm explicitly in the PR description.
- ED-11: further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry; stop and flag before expanding this packet.
