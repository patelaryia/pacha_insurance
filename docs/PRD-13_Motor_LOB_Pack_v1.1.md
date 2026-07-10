## PRD-13 — Motor LOB Pack & Modularity Proof (build spec)

> **v1.1** — incorporates the CTO decision round of 2026-07. Where this document conflicts with Section 0 (Shared Engineering Decisions) or Section 0.5 (Shared Agent Runtime), those files win. Anything underdetermined: follow ED-11 (never decide locally — route to the open-items register).

### 13.1 Purpose

Package every motor-specific element as one versioned, signed configuration artifact consumed by the generic platform — then prove the architecture by standing up a second pack (fire/property) with zero platform code changes beyond new document schemas. This PRD is the YC exhibit.

### 13.2 Pack repository format

```
packs/motor/
  pack.yaml                    # id: motor, version: 1.0.0 (semver),
                               # platform_min_version, display strings
                               # (e.g. external_expert: "Assessor")
  schema/fields.yaml           # field-dictionary extensions: vehicle.*, assessment.*,
                               # salvage.* — path, type, pii_class, validator
  schema/documents/*.yaml      # the 11 extraction schemas (PRD-01 §1.3) + reinspection_report,
                               # repair_invoice, eft_form
  rules/*.yaml                 # R-01..R-16 COP rules (❓ slots ship status: blocked_on_inputs)
  calcs/calcs.py               # C-01..C-07 — the ONLY code in a pack; sandboxed:
                               # imports restricted to stdlib decimal/datetime + platform Money;
                               # AST-checked at build (no IO, no network, no eval)
  templates/*.j2 + registry.yaml   # T-01..T-13 (+a/b/r variants); pending_capture flagged
  routing/authority_matrix.yaml
  workflows/cop_steps.yaml     # legal step sequences per capability (G-PROC source)
  checklists/claim_docs.yaml, surrender.yaml, items.yaml   # items.yaml (v1.1): registry of every
                               # checklist item {id, kind: document|physical|field_request,
                               # doc_type?, target_path?, physical: bool} — instantiation validates against it
  sla/definitions.yaml, holidays.yaml   # holidays.yaml (v1.1): KE public-holiday calendar (AR-3a / calendars)
  consistency/checks.yaml      # CC-1..CC-5
  manifest/approval_pack.yaml  # the 13-item merge manifest (PRD-08 §8.3)
  vendors/seed.yaml            # assessors, fee schedules, salvage-yard bidder seed
  value_maps/icon_codes.yaml   # dropdown maps captured by screenshot at embed
  autonomy/policies.yaml       # per-capability promotion overrides + max_level ceilings
  graders/thresholds.yaml      # per-field confidence overrides
  corpus/manifest.yaml         # S3 refs to the ≥100 anonymised cases + expected values
```

### 13.3 Build pipeline & registry

CI on the pack repo: meta-schema validation of every YAML → rule compilation to JSONLogic (fails on unresolvable field paths) → AST sandbox check + 100% unit coverage on calcs → StrictUndefined dry-render of every template against a fixture claim → dangling-reference scan (every template/rule/calc/doc-type id referenced anywhere must resolve) → full corpus golden run with per-capability scorecards → emit signed artifact (**v1.1 signing = tar + sha256 verified by the loader at boot, refusing mismatches, per ED-10; asymmetric/KMS signing deferred until packs cross an org boundary**) → `pack_registry(id, version, sha, built_at, status)` row.

**Version pinning & migration policy (v1.1: pinning is the SOLE versioning authority — `effective_from` in rule YAML is documentation metadata, never evaluated; see PRD-02 §2.2):** claims pin `pack@version` at creation (already in PRD-00 schema). Patch versions (bugfix, e.g. template typo) auto-apply to in-flight claims; minor/major versions apply to new claims only; migrating an in-flight claim is an explicit admin action, per-claim, ledgered with before/after pack refs. Rule changes therefore never silently alter an open claim's evaluation history — `rule_runs` keep the version that actually ran.

### 13.4 Pack conformance suite (generic — the contract any future pack signs)

A platform-owned test harness, parameterised by pack id, all green required for registry `status: publishable`: every rule has golden tests including boundary values · every calc at 100% coverage with Money-typed I/O · every registered doc schema has ≥ 3 corpus samples meeting its accuracy floors on the extraction engine · every capability has a COP step definition and ≥ 1 mapped critical grader · every checklist/manifest/SLA references only registered doc types and fields · all new fields carry PII classes · authority matrix covers amount range [0, ∞) with no gaps or overlaps.

### 13.5 Modularity proof — Fire/Property pack v0.1 (acceptance test for the whole architecture)

Scope of the minimal fire pack, chosen to exercise every extension point while dodging clinical-coding complexity: document schemas `fire_brigade_report`, `adjuster_report` (same generic shape as assessor_report: agreed amount, asset value, recommendation, flags), `contractor_quote/BOQ` (maps to repair_estimate: line items + total), `title_deed_or_lease`, `si_schedule`; rules: per-policy excess (flat or percentage — exercises calc parameterisation), constructive-total-loss gate (reinstatement > X% of SI — same rule shape as R-05), adjuster-appointment threshold; checklist: {claim form, fire brigade report, title/lease, SI schedule, photos, contractor quote}; approval manifest ~10 items; **no salvage auction** (module simply not bound — proving optional-module composition); display strings: external_expert → "Loss Adjuster", service_provider → "Contractor".

**The test, verbatim:** starting from a green motor pack, one engineer + Aryia build fire pack v0.1 with **zero platform code changes except** registering the five new document schemas + any new named validators; conformance suite green; then one pilot fire claim (real or realistic synthetic from a Mayfair fire file) processed end-to-end — intake → coverage triage → adjuster dispatch → report parse → reserve → approval pack signed and routed — **within 6 weeks of pack start**. Exit artifacts: the conformance report, the pilot claim's full event timeline, and a diff showing platform code touched (should be schemas only). That bundle is the modularity slide.

### 13.6 Acceptance

(1) Motor pack builds signed artifact, conformance green (❓slots visibly excepted with status, not silently skipped); (2) a template typo fix ships as 1.0.1 and reaches in-flight claims with no release; (3) an R-06 threshold change ships as 1.1.0, applies to new claims only, and an in-flight migration round-trips with ledger evidence; (4) the fire-pack test above, executed and archived.