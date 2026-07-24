# ADR-004 — Salvage auction provider strategy

- **Status:** accepted; provider selection pending RFI and due diligence
- **Decision date:** 2026-07-24
- **Owners:** Pacha CTO and repository owner
- **Supersedes:** Pacha's planned bidder-facing portal

## Context

Bidder identity, KYC, sealed bidding, portal security, auction hosting,
counter-offer transport and bidder support are established auction-platform
capabilities. They are not Pacha's claims-workflow differentiation and would
create a disproportionate external attack surface.

## Decision

Pacha will not build or operate a bidder-facing salvage portal.

Pacha retains:

- write-off, retain/surrender and auction eligibility decisions;
- surrender and lot-readiness gates;
- generation of an approved, minimal lot export;
- committee award authority;
- controlled import and verification of provider results;
- recovery and savings-ledger records; and
- reconciliation between the approved lot, provider result and recorded award.

The selected provider owns bidder authentication, bidder KYC, bid submission
and sealing, bidder-facing security, auction hosting, counter-offer transport,
bidder support and independent penetration testing of its service.

The first pilot may use controlled CSV/PDF export and CSV/PDF or manually
attested result import. An API is optional. No provider is selected without an
RFI, commercial/security due diligence, data-processing review and DPIA
acceptance.

Only the `lot_export` whitelist may cross the provider boundary. It contains
approved photos, vehicle make/model/year, registration, damage summary, yard
location and auction window instructions. Claim id, insured identity, policy
data, bank data and other claim facts are prohibited. Every outbound artifact
is scanned for insured and policy identifiers before release.

## Consequences

PRD-11 becomes a provider-integration and results-verification specification.
Portal routes, magic links, bidder accounts, bid submission, sealing,
counter-offer messaging and portal penetration tests are removed from Pacha's
build scope. Committee authority, never-guess behaviour, settlement gates and
the recovery ledger remain unchanged.
