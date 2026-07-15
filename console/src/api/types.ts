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
