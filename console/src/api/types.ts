export const REVIEW_TYPES = [
  "FIELD_VERIFY",
  "DOC_CLASSIFY",
  "DOC_SPLIT",
  "CONSISTENCY_FLAG",
  "DRAFT_RELEASE",
  "MODE_CONFIRM",
  "NOTE_REVIEW",
  "PACK_REVIEW",
  "EX_GRATIA",
  "EXCEPTION",
  "PROMOTION_SIGNOFF",
  "SAMPLE_REVIEW",
  "PASTE_READBACK_CHECK",
  "PROCEED_PARTIAL",
  "KYC_VERIFY",
  "EFT_MATCH",
  "REOPEN_PROMPT",
] as const;

export type ReviewType = (typeof REVIEW_TYPES)[number];
export type ReviewScope = "mine" | "pool" | "band";

export interface ReviewItem {
  id: string;
  claim_id: string | null;
  type: ReviewType;
  subtype: string | null;
  status: string;
  assigned_to: string | null;
  payload: Record<string, unknown>;
  workspace_layout: string;
  resolution_schema: string;
  sla: Array<Record<string, unknown>>;
}

export interface ReviewFilters {
  scope: ReviewScope;
  type?: ReviewType;
  status?: string;
  claim_id?: string;
}

export type ResolutionAction = "approve" | "edit_approve" | "reject";

export interface Claim360 {
  claim: {
    id: string;
    status: string;
    substatus: string | null;
    assigned_to: string | null;
    created_at: string;
    updated_at: string;
  };
  header: {
    insured: unknown | null;
    registration: unknown | null;
    amount_cents: bigint | null;
  };
  fields: Array<{
    path: string;
    value: unknown;
    value_type: string;
    verification_state: string;
    confidence: number | null;
    source_type: string;
    has_citation: boolean;
  }>;
  documents: Array<Record<string, unknown>>;
  financials: Array<{
    path: string;
    amount_cents: bigint;
    calc_run_id: string | null;
  }>;
  timeline: Array<Record<string, unknown>>;
  systems: Array<Record<string, unknown>>;
  communications: Array<Record<string, unknown>>;
  availability: Record<string, { status: string; owner: string }>;
}

export interface PackReadinessSource {
  kind: string;
  id: string;
  filename: string;
  received_at: string;
  sha256: string;
}

export interface PackReadinessItem {
  id: string;
  order: number;
  label: string;
  state: "ready" | "ambiguous" | "missing" | "invalid" | "pending_integration";
  required: boolean;
  waivable: boolean;
  sources: PackReadinessSource[];
  blockers: Array<{ code: string; item_id: string | null; detail: string }>;
}

export interface PackReadiness {
  claim_id: string;
  status: string;
  ready: boolean;
  fingerprint: string;
  checklists: { ready: boolean; blockers: Array<Record<string, unknown>> };
  fields: { ready: boolean; blockers: Array<Record<string, unknown>> };
  items: PackReadinessItem[];
  blockers: Array<{ code: string; item_id: string | null; detail: string }>;
}

export interface PackGeneration {
  status: string;
  note_status?: string;
  pack_version?: number;
  pack_event_id?: string;
  note_review_item_id?: string | null;
  review_item_id?: string;
  capability_id?: string;
}

export interface NoteSlot {
  slot: string;
  label: string;
  state: string;
  locked: boolean;
  display?: string;
  value?: unknown;
  value_type?: string;
  blocker?: string;
  citation_marker?: string;
  source_ref?: {
    field_id: string;
    path: string;
    version: number;
    provenance: Record<string, unknown>;
  } | null;
  evidence?: Array<{ id: string; check_id: string; status: string }>;
}

export interface NoteSection {
  template_slot: string;
  content: unknown;
  locked: boolean;
  numbers_used?: string[];
}

export interface ApprovalNoteWorkspace {
  review_id: string;
  review_status: string;
  claim_id: string;
  root_draft_id: string;
  current_draft: {
    id: string;
    version: number;
    status: string;
    body_sha256: string;
    edited_by: string | null;
    body: {
      sections: NoteSection[];
      blockers: Array<{ slot: string | null; state: string; detail: string }>;
      manager_rejection?: Record<string, unknown>;
      [key: string]: unknown;
    };
  };
  merged_pack: {
    event_id: string | null;
    version: number | null;
    sha256: string | null;
    content_url: string | null;
  };
  signed_note: { event_id: string; sha256: string; content_url: string } | null;
  sign_state: "unsigned" | "signing_pending" | "signed";
  autosave_seconds: number;
  commentary_slots: string[];
  editable_slots: string[];
  incident_summary_max_words: number;
  icon_note_entry: {
    id: string;
    status: string;
    blocked_on: string | null;
    fields: unknown[];
  };
  signable: boolean;
  blockers: Array<{ slot: string | null; state: string; detail: string }>;
}

export interface AutosaveResult {
  draft_id: string;
  version: number;
  body_sha256: string;
  parent_draft_id: string;
  review_id: string;
  recorded: boolean;
}

export interface ProjectionOperation {
  id: string;
  capability_id: string;
  system: string;
  mode: string;
  status: "live" | "pending_capture" | "blocked_on_inputs";
  blocked_on: string | null;
  owner_prd: string;
  version: string;
}

export interface ProjectionSummary {
  id: string;
  claim_id: string;
  operation: string;
  capability_id: string;
  mode: string;
  status: "queued" | "executing" | "verifying" | "completed" | "failed" | "diverged";
  snapshot_hash: string | null;
  definition_version: string | null;
  blocked_on: string | null;
  readback_paths: string[];
  attested_by: string | null;
  attested_at: string | null;
  paste_seconds: number | null;
  started_at: string | null;
  groups_done: Record<string, boolean>;
  created_at: string | null;
  completed_at: string | null;
}

export interface ProjectionSurface {
  operations: ProjectionOperation[];
  projections: ProjectionSummary[];
}

export interface PasteField {
  step_id: string;
  label: string;
  path: string;
  /** The exact server string the Clipboard API receives. Never reformatted. */
  copy_value: string;
  value_type: string;
  field_version: number | string;
}

export interface PasteGroup {
  id: string;
  label: string;
  done: boolean;
  fields: PasteField[];
}

export interface PasteReadbackField {
  label: string;
  path: string;
  required: boolean;
  format_status: "live" | "pending_capture";
  blocked_on: string | null;
}

export interface PasteAssistView {
  projection_id: string;
  claim_id: string;
  operation: string;
  definition_version: string;
  mode: string;
  status: ProjectionSummary["status"];
  groups: PasteGroup[];
  readback_fields: PasteReadbackField[];
  attestation_text: string;
  started_at: string | null;
  elapsed_seconds: number | null;
}

export interface Citation {
  claim_id: string;
  field_path: string;
  value: unknown;
  value_type: string;
  verification_state: string;
  document_id: string;
  page: number;
  bbox: readonly [number, number, number, number];
  document_url: string;
}

export interface SlaClockRow {
  clock_id: string;
  claim_id: string;
  definition_id: string;
  state: string;
  started_at?: string;
  breach_at: string | null;
  escalate_to_role: string;
}

export interface PortfolioTile {
  series_id: string;
  status: "live" | "pending_capture" | "unavailable";
  data: unknown;
}

export interface LedgerRow {
  seq: number;
  action: string;
  actor: string;
  claim_id: string | null;
  row_hash: string;
  before_hash?: string | null;
  after_hash?: string | null;
}

export interface CapabilityRow {
  id: string;
  current_level: string;
  max_level: string;
  pass_rate_window: number;
  consecutive_approvals: number;
  runs_to_promotion: number | null;
  sampling_rate: number;
  promotion_evidence?: Record<string, unknown>;
}

export interface PackRow {
  id: string;
  version: string;
  entries: Array<Record<string, unknown>>;
}

export interface ConsoleApi {
  listReviews(filters: ReviewFilters): Promise<ReviewItem[]>;
  resolveReview(
    reviewId: string,
    request: {
      action: ResolutionAction;
      schema_version: string;
      payload: Record<string, unknown>;
    },
  ): Promise<void>;
  getClaim360(claimId: string): Promise<Claim360>;
  getCitation(claimId: string, fieldPath: string): Promise<Citation>;
  getDocument?(documentUrl: string): Promise<ArrayBuffer>;
  getPackReadiness?(claimId: string): Promise<PackReadiness>;
  selectPackSources?(
    claimId: string,
    itemId: string,
    sources: Array<{ kind: string; id: string }>,
  ): Promise<unknown>;
  uploadPackItem?(claimId: string, itemId: string, file: File): Promise<unknown>;
  generatePack?(
    claimId: string,
    body: { readiness_fingerprint: string },
    idempotencyKey: string,
  ): Promise<PackGeneration>;
  getApprovalNote?(reviewId: string): Promise<ApprovalNoteWorkspace>;
  saveApprovalNote?(
    reviewId: string,
    body: {
      base_draft_id: string;
      base_body_sha256: string;
      commentary: Array<{ template_slot: string; content: string }>;
    },
    idempotencyKey: string,
  ): Promise<AutosaveResult>;
  getProjections?(claimId: string): Promise<ProjectionSurface>;
  getPasteAssist?(claimId: string, projectionId: string): Promise<PasteAssistView>;
  startPasteAssist?(claimId: string, projectionId: string): Promise<PasteAssistView>;
  setPasteGroup?(
    claimId: string,
    projectionId: string,
    groupId: string,
    done: boolean,
  ): Promise<PasteAssistView>;
  confirmPasteAssist?(
    claimId: string,
    projectionId: string,
    body: { attested: boolean; readback: Record<string, string> },
    idempotencyKey: string,
  ): Promise<ProjectionSummary>;
  getSlaBoard?(): Promise<{ clocks: SlaClockRow[] }>;
  escalateClocks?(clockIds: string[]): Promise<{
    results: Array<{
      clock_id: string;
      outcome: "escalated" | "blocked_on_inputs";
      blocked_on?: string;
    }>;
  }>;
  getPortfolio?(): Promise<{ tiles: PortfolioTile[] }>;
  seriesCsvUrl?(seriesId: string): string;
  searchLedger?(params: {
    actor?: string;
    action?: string;
    claim_id?: string;
    after_seq?: number;
    limit?: number;
  }): Promise<{ rows: LedgerRow[] }>;
  getPacks?(): Promise<{
    packs: PackRow[];
    adapter_health?: { status: string; owner: string };
    user_roles?: Record<string, unknown>;
  }>;
  getCapabilities?(): Promise<{ capabilities: CapabilityRow[] }>;
  promoteCapability?(
    id: string,
    body: { to_level: number | string; sign_offs: Array<Record<string, string>> },
  ): Promise<Record<string, unknown>>;
  listNotifications?(): Promise<{ items: Array<Record<string, unknown>> }>;
  markNotificationRead?(id: string): Promise<Record<string, unknown>>;
}
