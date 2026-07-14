# PACKET-11 — Console S-1/S-2 shell, SSO identity & citation viewer (PRD-04 slice 2 of 3)

> **Status:** issued · **Builder:** Codex per `AGENTS.md` · **Reviewer:** CTO per `CLAUDE.md`
> **Source spec:** `docs/PRD-04_Status_Console_and_Review_Queue_v1.1.md`
> §4.1 (React SPA, Entra ID SSO), §4.2 (RBAC server-side), §4.3 (S-1, S-2,
> keyboard focus rules, citation viewer), §4.5 acceptance scenarios 1/3/5 and
> NFRs; Section 0 ED-2 (frontend stack)/ED-6 (Entra ID OIDC)/ED-7 (vitest ≥70%)/
> ED-8/ED-11; PRD-00 §0.2/§0.4; register #20/#25/#90/#91/#92.
> Precedence: Section 0 → Section 0.5 → PRD-04/PRD-00 → this packet.
> **Depends on:** PACKET-10 merged on main (`ea02062`), including registers
> #96–#98.
> **Acceptance tests:** `tests/acceptance/test_packet_11_console.py` (pytest)
> and `tests/acceptance/console/packet_11_*.test.tsx` (vitest) — protected,
> failing by design until this packet is built.
> **Packet 12 (next):** PRD-04 slice 3 — S-3–S-6, AR-5 `notify/` websocket +
> email + digest, dashboard series windows (register #79), SLA board, admin
> screens, Playwright E2E leg, full X-Actor retirement for humans.

## 0. Slice position (PACKET-10 §0 map, unchanged)

This is slice 2 of the PACKET-10 CTO cut: the React SPA shell for **S-1
Review Queue** and **S-2 Claim 360**, Entra ID OIDC identity on the server,
the pdf.js citation viewer, and the §4.3 keyboard focus rules. The slice-1
API is consumed **unchanged**; identity is the only server-side layer that
moves. Register #90 closes here: the 17 workspace-layout ids shipped as pack
data in PACKET-10 bind to React components in this packet.

## 1. Scope

**In:**

1. **Console SPA scaffold** at `/console` (ED-1 monorepo slot): React 18 +
   TypeScript + Vite + TanStack Query + Tailwind + pdf.js, all dependencies
   bundled locally — the built app makes **zero** requests to any origin other
   than its own API (`/api/console/*` + existing platform routes). Node 20 LTS;
   vitest + Testing Library + jsdom per ED-7 (≥70% coverage, enforced in
   `vitest.config.ts`). The protected specs under `tests/acceptance/console/`
   are included in the vitest run via the config `include` glob and the
   `@console` → `console/src` alias.
2. **S-1 Review Queue** (officer home). Left: filterable `review_items` list
   from `GET /reviews` (scope mine/pool, type, status filters); SLA chip from
   the item's `sla` array. Right: item workspace — the PACKET-10 contract
   registry's `workspace_layout` id selects the component (register #90);
   types whose producers do not exist yet render the generic workspace. Every
   item shows agent output + citations + exactly three primary actions
   (**Approve / Edit→Approve / Reject**). Reject requires a free-text reason;
   the reason **enum** control renders visibly `pending_capture` (register
   #91) — no invented codes. Resolution POSTs the PACKET-10 body
   `{action, schema_version, payload}` and the resolver still derives
   `resolution` — the SPA never sends a `resolution` value.
3. **Keyboard focus rules — §4.3 verbatim.** `a` approve, `e` edit, `r`
   reject, `j/k` navigate, implemented as roving focus on the list/item
   container; **disabled whenever focus is in any input/textarea/
   contenteditable**; `a/e/r` require an explicitly focused item; `Esc`
   returns focus to the list. **No global keydown handlers.**
4. **S-2 Claim 360.** Header (claim id, insured, reg, amount) + **status
   rail**: FSM states as a horizontal stepper fed by `GET /api/console/fsm`
   (topology derived from `claim_core.fsm` data — never a hard-coded state
   list); DECLINED renders as a full-width red banner. The banner's reopen
   action renders **disabled with a visible `blocked_on_inputs` reason** — no
   reopen endpoint exists until PRD-05 REOPEN_PROMPT (register #25; proposed
   #105). Tabs:
   - *Overview* — parties, key fields with confidence + verification badges
     (both are `claim_fields` columns).
   - *Documents* — received documents from `GET /claims/{id}/documents`;
     the requested/verified/waived checklist tracker and chase history are
     PRD-06 producers: render the slot with a visible blocked state
     (proposed #105), never invented rows.
   - *Fields & Citations* — current fields table; clicking a cited field
     opens the **citation viewer** (§1.5). Inline verify/correct submits
     through the existing `PATCH /claims/{claim_id}/fields` human
     `human_verified` write — no new write path.
   - *Financials* — figures linked to their `calc_runs` rows via
     `GET /api/console/claims/{id}/calc-runs`.
   - *Timeline* — `GET /claims/{id}/timeline`, human-readable, **EAT
     (UTC+3) rendering** per ED-2 regardless of browser timezone.
   - *Systems* — PRD-09 projections do not exist: visible "no projection
     data" blocked state (proposed #105).
   - *Communications* — threaded render of `communications` rows; empty
     state until PRD-06 writes rows.
5. **Citation viewer.** pdf.js page render (bundled worker, no CDN), bbox
   highlight overlay, value panel, verify/correct inline. The page renderer
   is an injectable seam
   (`renderPage(blobUrl, page, scale) → Promise<{width, height}>`) so the
   protected vitest specs exercise overlay geometry without a real pdf.js
   canvas in jsdom. Bbox space is the `claim_fields.source_ref.bbox`
   produced by doc_intel: `[x0, y0, x1, y1]`, top-left origin, rendered-page
   pixels at scale 1; overlay = `left x0·s, top y0·s, width (x1−x0)·s,
   height (y1−y0)·s`. Document bytes come from
   `GET /api/console/documents/{document_id}/blob`.
6. **Server: `build_console(app, ...)` in `platform/review_queue/`** (PRD-04
   stays one package, ED-1):
   - **Static serving** of the built SPA (`console/dist` by default,
     `dist_path` override for tests) at `/console/`.
   - **Read-only console API** (proposed #99) — no new tables, no
     migration, projections of existing rows only:
     `GET /api/console/config` (auth mode, issuer/client id when configured —
     never a secret), `GET /api/console/fsm` (primary path, write-off path,
     terminal states, derived from `claim_core.fsm`),
     `GET /api/console/documents/{document_id}/blob` (original bytes, stored
     mime, `ETag` = stored sha256, 404 unknown),
     `GET /api/console/claims/{claim_id}/calc-runs` (`{"runs": [...]}`).
     **No POST/PUT/PATCH/DELETE exists under `/api/console/`.**
   - **OIDC bearer identity (ED-6).** `oidc=OIDCConfig(issuer, audience,
     jwks)` enables bearer mode: RS256 `Authorization: Bearer` tokens
     validated offline against the injected JWKS (signature, `iss`, `aud`,
     `exp`); the token's `oid` claim maps through org config
     `packs/motor/routing/users.yaml` (or the injected `users=` dict) to a
     `user:<ULID>` actor (proposed #101), which then flows through the
     **unchanged** PACKET-10 role/band authorisation. Invalid/expired/
     mis-audienced tokens → 401 `TOKEN_INVALID`. Valid token, unmapped `oid`
     → 403 `FORBIDDEN_ROLE` + `authz.denied` ledgered (existing ACTION_MAP —
     no new ledger actions). A `users.yaml` mapping to any non-`user:*`
     actor fails the build with `ValueError` (fail closed).
   - **Bearer-mode header lockout.** With OIDC enabled, any request carrying
     `X-Actor: user:*` without a valid bearer token → 401 `TOKEN_REQUIRED`;
     `agent:*`/`system` X-Actor traffic is untouched (internal callers).
     With `oidc=None` (test/dev mode) the PACKET-10 header contract is
     preserved verbatim and the SPA shows a visible dev-identity banner
     (proposed #102).
   - Real Entra tenant/client ids are uncaptured: `packs/motor/routing/
     oidc.yaml` ships `pending_capture` values and `/api/console/config`
     reports `sso_status` so the SPA renders a visible "SSO not configured"
     screen — never a silent fallback in a production build (proposed #100).

**Out / blocked:** S-3–S-6 (slice 3); websocket/email/digest notifications
(AR-5, slice 3); portfolio tiles + series windows (#79); SLA board; admin/user
management UI (users.yaml is hand-edited org config until S-6); Playwright E2E
and the 1366×768 viewport check (slice 3 leg, proposed #104); reopen execution
(#25, PRD-05); checklist tracker + chase history data (PRD-06); Systems tab
data (PRD-09); reject-reason enum values (#91); scenario 2 (5s propagation —
needs slice-3 websocket); the §4.5 timing NFRs and human-timed scenario 3 and
corpus-sampled scenario 5 (proposed #103 — live-ops/corpus gates, CI ships
synthetic stand-ins only); full X-Actor retirement for humans (slice 3,
proposed #102).

## 2. Binding spec quotes (implement verbatim)

PRD-04 §4.3:

> "**Keyboard (v1.1 focus rules):** `a` approve, `e` edit, `r` reject, `j/k`
> navigate — implemented as roving focus on the list/item container,
> **disabled whenever focus is in any input/textarea/contenteditable**;
> `a/e/r` require an explicitly focused item; `Esc` returns focus to the
> list. No global keydown handlers."

> "Every item shows agent output + citations + exactly three primary actions:
> **Approve / Edit→Approve / Reject (reason required, enum + free text)**."

PRD-04 §4.3 S-2:

> "**status rail** (FSM states as a horizontal stepper; DECLINED renders as a
> full-width red banner with reopen action — declines are structurally
> impossible to miss)"

> "_Fields & Citations_ (table of current fields; clicking any field opens
> **the citation viewer**: pdf.js page render, bbox highlight, value panel,
> verify/correct inline)"

PRD-04 §4.2:

> "A user's approval band comes from role; the console never lets anyone
> approve outside band (server 403)."

Section 0 ED-2:

> "Frontend: React 18 + TypeScript + Vite, TanStack Query, Tailwind, pdf.js
> (citation viewer). ... All timestamps UTC in storage, rendered EAT (UTC+3)
> in UI."

Section 0 ED-6:

> "SSO via Microsoft Entra ID (OIDC) — Mayfair is an Outlook shop, so users
> exist already. RBAC roles defined in PRD-04."

## 3. Deliverable

```text
console/
  package.json package-lock.json vite.config.ts vitest.config.ts
  tsconfig.json tailwind.config.* index.html
  src/
    api.ts                       # ConsoleApi type + makeApi(baseUrl, token)
    auth.tsx                     # MSAL auth-code+PKCE; config from /api/console/config
    keyboard.ts                  # roving-focus hook (no global handlers)
    format.ts                    # EAT timestamps; KES money from integer cents
    screens/ReviewQueue.tsx      # S-1
    screens/Claim360.tsx         # S-2
    components/CitationViewer.tsx
    components/StatusRail.tsx
    workspaces/                  # 17 layout-id bindings (register #90)
platform/review_queue/
  console.py                     # build_console(app, ...): static + read API
  sso.py                         # OIDCConfig, JWKS validation, oid→actor map
packs/motor/routing/users.yaml   # oid → user:<ULID> org config
packs/motor/routing/oidc.yaml    # issuer/client_id/audience — pending_capture
```

**Authorised changes outside `/console` + `/platform/review_queue`: exactly
two, nothing else.** (1) `requirements.txt` gains `PyJWT>=2.9` (server-side
token verification; already added by this packet's cut — verify, don't
duplicate). (2) `packs/motor/routing/` gains the two org-config files above.
**Zero `claim_core` changes**: blob bytes come from `app.state.blob_store`,
rows via existing root exports; if the builder believes a `claim_core` change
is needed, stop and flag (ED-11). No migration in this packet — a new
column/table is a defect without a register entry.

`.github/`, `tools/ci/`, protected acceptance files untouched by the builder.
The CI `console` job (Node 20: `npm ci`, `npm run build`,
`npm test -- --run --coverage`) is already wired by this packet's cut.

### 3.1 Pinned public surface (acceptance relies on exactly this)

Python:

```python
from review_queue import build_console
from review_queue.sso import OIDCConfig

console = build_console(
    app,
    roles=None,       # as build_review_queue; None loads packs/motor/routing/roles.yaml
    users=None,       # dict oid -> "user:<ULID>"; None loads packs/motor/routing/users.yaml
    oidc=None,        # None = header mode (PACKET-10 contract intact);
                      # OIDCConfig(issuer=str, audience=str, jwks=dict) = bearer mode
    dist_path=None,   # None = console/dist
)

# GET /console/                    -> SPA index.html (from dist_path)
# GET /api/console/config          -> {"auth_mode": "header"|"oidc",
#                                      "sso_status": "configured"|"pending_capture",
#                                      + issuer/client_id when configured; never a secret}
# GET /api/console/fsm             -> {"primary_path": [...], "write_off_path": [...],
#                                      "terminal": [...]}
# GET /api/console/documents/{id}/blob        -> bytes, stored mime, ETag=sha256 | 404
# GET /api/console/claims/{id}/calc-runs      -> {"runs": [...]}
# 401 {"code": "TOKEN_INVALID"|"TOKEN_REQUIRED"}
# 403/409/422 codes unchanged from PACKET-10
```

Frontend modules (protected vitest specs import exactly these):

```ts
import { ReviewQueue } from "@console/screens/ReviewQueue";      // props: { api }
import { Claim360 } from "@console/screens/Claim360";           // props: { api, claimId }
import { CitationViewer } from "@console/components/CitationViewer";
// props: { blobUrl, citation: {page, bbox}, scale?, renderPage? }
```

Pinned test ids: `review-list`, `review-item-<id>`, `reject-reason`,
`reject-submit`, `declined-banner` (role="alert"; reopen control =
`reopen-action`, disabled, `data-blocked-reason="blocked_on_inputs"`),
`status-rail`, `status-step-<STATE>` (current step `aria-current="step"`),
`citation-highlight`, `field-money-<path>`. `ConsoleApi` methods used by the
specs: `listReviews`, `getReview`, `resolveReview(id, body)`, `getClaim`,
`getTimeline`, `listDocuments`, `getFinancials`, `getFsmStates`,
`getDocumentBlobUrl`.

## 4. CTO decisions (D-x) and proposed register entries

Builder appends these to the register with the implementation PR, PACKET-10
pattern:

- **#99 — PRD-04 defines screens, not an HTTP contract.** Minimal read-only
  `/api/console/*` endpoints designed locally; projections of existing rows
  only, zero write surface, no DDL. Later console endpoints need a register
  entry each.
- **#100 — Entra tenant/client registration uncaptured** (nothing in the
  master register covers it — new capture item, owner Aryia → Mayfair IT,
  same written request stream as item 1). `oidc.yaml` ships
  `pending_capture`; token validation is proven offline via injected JWKS;
  first real-tenant login is a go-live gate, not CI-dischargeable.
- **#101 — no PRD maps an Entra identity to a platform actor** (`roles.yaml`
  keys are `user:<ULID>`, tokens carry `oid` GUIDs). `users.yaml` org config
  maps `oid → user:<ULID>`; unmapped `oid` = 403, never auto-provisioned;
  the S-6 user-management screen (slice 3) becomes the maintained surface.
- **#102 — X-Actor retirement is staged, not atomic.** Registers #20/#92 say
  SSO replaces the header, but protected PACKET-01–10 suites drive human
  writes via X-Actor and must keep passing unmodified. Bearer mode locks out
  header `user:*` identities wherever `build_console` is installed with OIDC;
  `oidc=None` preserves the header contract for tests/dev with a visible
  banner. Production config is bearer-only; the header path for humans is
  deleted in PACKET-12.
- **#103 — scenarios 1/3/5 and §4.5 NFR timings are not CI-dischargeable**
  (mirror of #36/#55/#83): scenario 1 end-to-end needs Phase-2 agents — CI
  proves the console-only officer flow over the existing substrate; scenario
  3 is a human-timed 20-item test — pilot gate; scenario 5's 50 sampled
  fields need the item-7 corpus — CI ships a synthetic bbox-fidelity test;
  p95 <400ms and <1.5s render are live-ops gates. Packet-11 green does not
  discharge them.
- **#104 — frontend toolchain under-pinned by ED-2.** Pin: Node 20 LTS,
  vitest + Testing Library + jsdom for the protected specs (vitest is
  ED-7-named); Playwright E2E + 1366×768 viewport checks land with the
  PACKET-12 leg. Exact dependency pins live in `console/package-lock.json`.
- **#105 — S-2 tabs whose producers are unbuilt** (checklist/chase = PRD-06,
  projections = PRD-09, reopen = PRD-05 per #25): ship the slot, the status,
  and the visible blocked state (guide §6); never invented rows, never an
  enabled control that cannot execute.

## 5. Builder guardrails

- **Keyboard rules verbatim** — roving focus only; no `document`/`window`
  keydown listeners anywhere in `console/src`.
- **Closed enum untouched** — the SPA renders the 17 types + `EXCEPTION`
  subtypes; no client-side type invention; unknown layout id → generic
  workspace, never a crash or a dropped item.
- **Never guess** — pending reason enum renders `pending_capture`; missing
  SSO config renders the blocked screen; unbuilt tabs render blocked states;
  the SPA never fabricates a value the API did not return.
- **No new write paths** — the SPA writes only via existing endpoints
  (`/reviews/{id}/resolve`, `PATCH /claims/{id}/fields`, existing FSM
  routes); `/api/console/*` is read-only; resolve stays human-only
  (`agent:*`/`system` 403 unchanged).
- **Identity is config, not client-asserted** — role/band come from the
  server per PACKET-10; the SPA hides out-of-band actions but the server 403
  remains the enforcement (PRD-04 §4.2). Tokens are handled by MSAL
  (auth-code + PKCE); no hand-rolled token storage; no token in a URL.
- **Self-contained bundle** — pdf.js worker and all assets served from own
  origin; zero CDN/external requests; CSP-compatible build.
- **Money is integer cents end-to-end** — the SPA formats `KES` from integer
  cents for display only and performs **no** money arithmetic; ED-8 stays a
  backend property.
- **EAT rendering (ED-2)** and British English in all UI prose.
- **Ledger single-writer unchanged** — `authz.denied`/`review.resolved` flow
  through the existing consumer; no new ACTION_MAP entries.
- All PACKET-01–10 suites keep passing unmodified.

## 6. Definition of done (ED-7/ED-7a)

- `tests/acceptance/test_packet_11_console.py` passes unmodified on SQLite
  and PostgreSQL legs; `tests/acceptance/console/packet_11_*.test.tsx` pass
  unmodified in the CI `console` job.
- CI fully green: python job + new console job (build + vitest, coverage
  ≥70% enforced in config); ruff, money-float lint, banned-calls green
  (`console/` is outside their scope by construction — do not add Python
  there).
- ≥80% unit coverage on new `platform/review_queue/` modules; OpenAPI
  regenerated including `/api/console/*`; no migration (none permitted).
- Runbook content flagged in the PR description (CTO owns protected docs):
  SSO outage triage, unmapped-user triage, blob-endpoint 404s, dev-mode
  banner semantics.
- Grader coverage: UI-only slice, no new OutputType; `grader_map.yaml`
  unchanged — confirm explicitly in the PR description.
- ED-11: any further ambiguity ⇒ narrowest safe behaviour + proposed register
  entry; stop and flag before expanding this packet.
