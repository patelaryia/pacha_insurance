# Status console and Entra ingress runbook

## Scope

This runbook covers PACKET-11: the S-1 review queue, S-2 Claim 360 and citation
viewer, and the Microsoft Entra staff identity boundary. S-3–S-6,
notifications, checklist/chase producers, projection producers and reopen
execution remain visibly unavailable until their owning PRDs are installed.

## Required deployment configuration

The API requires these environment values when `install_console` is called
without an injected test verifier:

- `PACHA_ENTRA_TENANT_ID`: canonical lower-case tenant GUID.
- `PACHA_ENTRA_API_AUDIENCE`: the exact access-token audience.
- `PACHA_ENTRA_AUTHORITY`: optional, but when set must equal the tenant-specific
  `https://login.microsoftonline.com/<tenant>/v2.0` authority.

The browser requires `VITE_ENTRA_TENANT_ID`, `VITE_ENTRA_CLIENT_ID`,
`VITE_ENTRA_API_AUDIENCE`, `VITE_ENTRA_API_SCOPE`,
`VITE_ENTRA_REDIRECT_URI`, `VITE_ENTRA_AUTHORITY` and `VITE_API_BASE_URL` at
build/deployment time. No production value is committed. MSAL uses the
authorisation-code flow with PKCE and session storage; local storage is not
used.

Organisation identity data lives in deployment-managed copies of
`packs/motor/routing/identities.yaml` and `roles.yaml`. Identity keys are exact
`<tenant-guid>:<object-guid>` pairs and values are internal `user:<ULID>`
actors. Roles are looked up independently. Duplicate actor targets, malformed
keys, unmapped actors and actors without roles fail closed.

## Startup and JWKS failure

1. Confirm the tenant, authority and audience values are present and exact.
2. Confirm outbound HTTPS can reach the tenant OIDC metadata URL and its
   advertised HTTPS JWKS URL.
3. An unknown signing `kid` causes one bounded JWKS refresh. Persistent unknown
   keys, malformed metadata, timeouts or invalid signatures return
   `401 INVALID_TOKEN`; tokens are never decoded as an authentication fallback.
4. If metadata/JWKS is unavailable, restore outbound connectivity or Entra
   service health. Do not disable signature, issuer, audience, expiry or
   not-before validation.

## 401 and 403 triage

- `401 AUTHENTICATION_REQUIRED`: no usable bearer header reached the API. Check
  MSAL account state, requested API scope and reverse-proxy header forwarding.
- `401 INVALID_TOKEN`: check signature key rotation, exact audience, tenant,
  issuer, expiry and workstation clock. Bearer material must not be logged.
- `403 IDENTITY_NOT_MAPPED`: add the exact immutable `(tid, oid)` pair through
  the deployment identity change process. Never map by email, display name,
  group or token role.
- `403 FORBIDDEN_ROLE`: verify independent org role configuration. Finance and
  admin intentionally have no S-1/S-2 access in this packet; auditors are
  read-only.
- `400 ACTOR_HEADER_FORBIDDEN`: a proxy or client is still sending the retired
  human `X-Actor` transport. Remove it; browsers send only bearer tokens. Once
  console ingress is installed this check applies before every application
  route, including legacy claim, field, transition, decline, document and event
  routes, so none can be used as an actor-spoofing side door.

Resolution authorisation remains server-side and narrower than read access.
Band or role denials are recorded through the event spine and single-writer
ledger consumer.

## Citation and PDF refusal

`409 CITATION_UNAVAILABLE` is expected when there is no valid extraction or
human-verified review citation, the cited document belongs to another claim,
the page/bbox is invalid, or the normalised PDF is absent/corrupt. The resolver
walks immutable field versions newest-to-oldest, shows the current field value,
and uses only exact stored same-claim evidence. It never rematches text or
invents a box.

DocIntel keeps PII review candidates in private
`review-candidates/<document-id>/` blobs and stores only a redacted event
placeholder. Authenticated review reads hydrate that value in memory and never
return the blob key. `candidate_status=blocked_on_inputs` means the private
candidate is absent or the reference failed its strict prefix check: restore
the immutable blob from the same pipeline artifact or reject the item; never
paste a storage key into the browser. `doc.extract` attribution and field type
come from pack/dictionary data, not browser defaults.

Only `normalised/<document-id>.pdf` is addressable. The document row must exist
on a real claim and the bytes must parse as a non-empty PDF. Responses are
`application/pdf` with `Cache-Control: private, no-store` and
`X-Content-Type-Options: nosniff`. Original blob keys and arbitrary storage
paths are not accepted by the route.

## Frontend rollback

The console is a static Vite build. Retain the prior immutable asset release.
To roll back, point the static origin at that release while leaving the API
identity boundary installed; never restore a browser-supplied actor header.
Clear the CDN's HTML entry only after the prior asset set is present. Existing
session tokens remain in session storage and are revalidated by the API.

## Approval-pack surfaces (PACKET-19)

**S-2 readiness card.** The 13-row manifest card lives in the Claim-360 *Documents* tab
(PRD-04 §4.3 fixes the seven tab names, so no eighth tab exists — register #256). Explicit
rows offer only same-claim documents and communications Claim 360 already returned; items 12
and 13 accept a PDF upload with a visible progress state and an `role="alert"` error. Generate
submits the current readiness fingerprint and an idempotency key. A `READINESS_STALE` 409
refreshes the card and reports no success; a staged generation is reported as staged, never as
completed.

**NOTE_REVIEW workspace.** Left: the semantic note editor with read-only computed and
verification rows that show their state and link their citation/provenance; only the three
commentary text areas are editable. Right: the merged pack through the authenticated artifact
endpoint and pdf.js. Blockers stay beside **Sign** and cannot be dismissed. Autosave fires
after the pack-configured `autosave_seconds` of dirty state and on blur, and reports
`Saving… / Saved at <EAT> / Save failed`. A `409 STALE_NOTE_DRAFT` reloads the latest server
version and preserves the local text in a labelled recovery panel. Approve is labelled
**Sign**, Edit→Approve is **Save & Sign** (autosave first, then sign that exact saved id and
hash — prose never travels through the resolution endpoint), and Reject stays reason-required.
Keyboard shortcuts remain disabled inside inputs, textareas and contenteditable; no global key
handler is added.

**S-3 approval workspace.** The band queue lists only `PACK_REVIEW{approval_pack}` items for
the actor's exact role. The merged pack and the signed note render side by side from the
authenticated artifact route. The routed figure is rendered from integer cents with its
provenance, and the T-03 state is visible for >KES 4M. Outside-band, stale-route and
blocked-side-effect errors are explicit and never optimistically remove the item. A legacy
`PACK_REVIEW` with no `approval_pack` subtype keeps the explicit unavailable state, because it
genuinely carries no PRD-08 artifacts.

## Verification and live gates

Run before release:

```text
ruff check .
python tools/ci/money_float_lint.py
python tools/ci/banned_calls.py
pytest -q
cd console
npm run lint
npm run build
npm run test:coverage
```

The browser suite mechanically checks 50 bbox transforms, but production
acceptance still requires 50 sampled production fields with zero visual misses.
Time 20 human FIELD_VERIFY resolutions and require median handle time at or
below 10 seconds. The PRD-08 gates are equally live: pack generation under two
minutes, officer review and sign at or below eight minutes, side-by-side manager
sign-off against the current Mayfair PDF, and Chromium/S3 Object-Lock
certification. Fake-timer autosave assertions and synthetic screenshots do not
discharge any of them. Measure queue and Claim-360 p95 below 400 ms and citation page
render below 1.5 seconds on the target Mayfair desktop/network. Record these as
live trial evidence; synthetic CI results do not discharge the human or p95
gates.
