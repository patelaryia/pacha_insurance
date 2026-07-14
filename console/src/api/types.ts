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
export type ReviewScope = "mine" | "pool";

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
}
