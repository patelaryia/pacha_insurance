import type {
  ApprovalNoteWorkspace,
  AutosaveResult,
  Citation,
  Claim360,
  ConsoleApi,
  ReviewFilters,
  ReviewItem,
  ResolutionAction,
  LedgerRow,
  PackGeneration,
  PackReadiness,
  PasteAssistView,
  PortfolioTile,
  ProjectionSummary,
  ProjectionSurface,
  SlaClockRow,
  CapabilityRow,
  PackRow,
} from "./types";
import { parseLossless, stringifyLossless, toBigInt, toSafeNumber } from "../lib/json";

interface ClientOptions {
  baseUrl: string;
  getAccessToken: () => Promise<string>;
}

export class ConsoleApiClient implements ConsoleApi {
  readonly baseUrl: string;
  private readonly getAccessToken: () => Promise<string>;

  constructor(options: ClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.getAccessToken = options.getAccessToken;
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const token = await this.getAccessToken();
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token}`);
    headers.set("Content-Type", "application/json");
    const response = await fetch(`${this.baseUrl}${path}`, { ...init, headers });
    if (!response.ok) {
      const text = await response.text();
      let body: Record<string, unknown> = {};
      try {
        const parsed = parseLossless(text);
        if (typeof parsed === "object" && parsed !== null) body = parsed as Record<string, unknown>;
      } catch {
        // Non-JSON gateway errors retain the HTTP status as the actionable code.
      }
      throw {
        code: typeof body.code === "string" ? body.code : `HTTP_${response.status}`,
        detail: typeof body.detail === "string" ? body.detail : "Request failed",
      };
    }
    return response;
  }

  private async json(response: Response): Promise<unknown> {
    return parseLossless(await response.text());
  }

  async listReviews(filters: ReviewFilters): Promise<ReviewItem[]> {
    const query = new URLSearchParams(
      Object.entries(filters).map(([key, value]) => [key, String(value)]),
    );
    const response = await this.request(`/reviews?${query}`);
    const body = (await this.json(response)) as { items: ReviewItem[] };
    return body.items;
  }

  async resolveReview(
    reviewId: string,
    request: {
      action: ResolutionAction;
      schema_version: string;
      payload: Record<string, unknown>;
    },
  ): Promise<void> {
    await this.request(`/reviews/${encodeURIComponent(reviewId)}/resolve`, {
      method: "POST",
      body: stringifyLossless(request),
    });
  }

  async getClaim360(claimId: string): Promise<Claim360> {
    const response = await this.request(`/console/claims/${encodeURIComponent(claimId)}/360`);
    const body = (await this.json(response)) as Claim360;
    return {
      ...body,
      header: {
        ...body.header,
        amount_cents: body.header.amount_cents === null
          ? null
          : toBigInt(body.header.amount_cents, "header.amount_cents"),
      },
      fields: body.fields.map((field) => ({
        ...field,
        value: field.value_type === "money"
          ? toBigInt(field.value, `${field.path}.value`)
          : field.value,
        confidence: field.confidence === null
          ? null
          : toSafeNumber(field.confidence, `${field.path}.confidence`),
      })),
      financials: body.financials.map((row) => ({
        ...row,
        amount_cents: toBigInt(row.amount_cents, `${row.path}.amount_cents`),
      })),
    };
  }

  async getCitation(claimId: string, fieldPath: string): Promise<Citation> {
    const response = await this.request(
      `/console/claims/${encodeURIComponent(claimId)}/fields/${encodeURIComponent(fieldPath)}/citation`,
    );
    const body = (await this.json(response)) as Citation;
    return {
      ...body,
      value: body.value_type === "money"
        ? toBigInt(body.value, `${body.field_path}.value`)
        : body.value,
      page: toSafeNumber(body.page, "citation.page"),
      bbox: body.bbox.map((coordinate, index) =>
        toSafeNumber(coordinate, `citation.bbox[${index}]`),
      ) as unknown as Citation["bbox"],
    };
  }

  async getDocument(documentUrl: string): Promise<ArrayBuffer> {
    const response = await this.request(documentUrl);
    return response.arrayBuffer();
  }

  async getPackReadiness(claimId: string): Promise<PackReadiness> {
    return (await this.json(await this.request(
      `/claims/${encodeURIComponent(claimId)}/approval-pack/readiness`,
    ))) as PackReadiness;
  }

  async selectPackSources(
    claimId: string,
    itemId: string,
    sources: Array<{ kind: string; id: string }>,
  ): Promise<unknown> {
    return this.json(await this.request(
      `/claims/${encodeURIComponent(claimId)}/approval-pack/manifest/`
      + `${encodeURIComponent(itemId)}/sources`,
      { method: "PUT", body: stringifyLossless({ sources }) },
    ));
  }

  async uploadPackItem(claimId: string, itemId: string, file: File): Promise<unknown> {
    // Multipart sets its own boundary, so this call cannot use `request`'s
    // JSON content type.
    const token = await this.getAccessToken();
    const form = new FormData();
    form.append("file", file, file.name);
    const response = await fetch(
      `${this.baseUrl}/claims/${encodeURIComponent(claimId)}/approval-pack/manifest/`
      + `${encodeURIComponent(itemId)}/upload`,
      { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: form },
    );
    if (!response.ok) {
      throw {
        code: `HTTP_${response.status}`,
        detail: "The upload was refused",
      };
    }
    return this.json(response);
  }

  async generatePack(
    claimId: string,
    body: { readiness_fingerprint: string },
    idempotencyKey: string,
  ): Promise<PackGeneration> {
    return (await this.json(await this.request(
      `/claims/${encodeURIComponent(claimId)}/approval-pack/generate`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: stringifyLossless(body),
      },
    ))) as PackGeneration;
  }

  async getApprovalNote(reviewId: string): Promise<ApprovalNoteWorkspace> {
    return (await this.json(await this.request(
      `/reviews/${encodeURIComponent(reviewId)}/approval-note`,
    ))) as ApprovalNoteWorkspace;
  }

  async saveApprovalNote(
    reviewId: string,
    body: {
      base_draft_id: string;
      base_body_sha256: string;
      commentary: Array<{ template_slot: string; content: string }>;
    },
    idempotencyKey: string,
  ): Promise<AutosaveResult> {
    return (await this.json(await this.request(
      `/reviews/${encodeURIComponent(reviewId)}/approval-note/draft`,
      {
        method: "PUT",
        headers: { "Idempotency-Key": idempotencyKey },
        body: stringifyLossless(body),
      },
    ))) as AutosaveResult;
  }

  private projectionPath(claimId: string, projectionId?: string): string {
    const base = `/console/claims/${encodeURIComponent(claimId)}/projections`;
    return projectionId === undefined
      ? base
      : `${base}/${encodeURIComponent(projectionId)}/paste-assist`;
  }

  async getProjections(claimId: string): Promise<ProjectionSurface> {
    return (await this.json(
      await this.request(this.projectionPath(claimId)),
    )) as ProjectionSurface;
  }

  async getPasteAssist(claimId: string, projectionId: string): Promise<PasteAssistView> {
    return (await this.json(
      await this.request(this.projectionPath(claimId, projectionId)),
    )) as PasteAssistView;
  }

  async startPasteAssist(claimId: string, projectionId: string): Promise<PasteAssistView> {
    return (await this.json(await this.request(
      `${this.projectionPath(claimId, projectionId)}/start`,
      { method: "POST" },
    ))) as PasteAssistView;
  }

  async setPasteGroup(
    claimId: string,
    projectionId: string,
    groupId: string,
    done: boolean,
  ): Promise<PasteAssistView> {
    return (await this.json(await this.request(
      `${this.projectionPath(claimId, projectionId)}/groups/${encodeURIComponent(groupId)}`,
      { method: "PUT", body: stringifyLossless({ done }) },
    ))) as PasteAssistView;
  }

  async confirmPasteAssist(
    claimId: string,
    projectionId: string,
    body: { attested: boolean; readback: Record<string, string> },
    idempotencyKey: string,
  ): Promise<ProjectionSummary> {
    return (await this.json(await this.request(
      `${this.projectionPath(claimId, projectionId)}/confirm`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: stringifyLossless(body),
      },
    ))) as ProjectionSummary;
  }

  async getSlaBoard(): Promise<{ clocks: SlaClockRow[] }> {
    return (await this.json(await this.request("/console/ops/sla-board"))) as {
      clocks: SlaClockRow[];
    };
  }

  async escalateClocks(clockIds: string[]): Promise<{
    results: Array<{
      clock_id: string;
      outcome: "escalated" | "blocked_on_inputs";
      blocked_on?: string;
    }>;
  }> {
    return (await this.json(await this.request("/console/ops/sla-board/escalate", {
      method: "POST",
      body: stringifyLossless({ clock_ids: clockIds }),
    }))) as {
      results: Array<{
        clock_id: string;
        outcome: "escalated" | "blocked_on_inputs";
        blocked_on?: string;
      }>;
    };
  }

  async getPortfolio(): Promise<{ tiles: PortfolioTile[] }> {
    return (await this.json(await this.request("/console/ops/portfolio"))) as {
      tiles: PortfolioTile[];
    };
  }

  seriesCsvUrl(seriesId: string): string {
    return `${this.baseUrl}/console/ops/portfolio/${encodeURIComponent(seriesId)}.csv`;
  }

  async searchLedger(params: {
    actor?: string;
    action?: string;
    claim_id?: string;
    after_seq?: number;
    limit?: number;
  }): Promise<{ rows: LedgerRow[] }> {
    const query = new URLSearchParams(
      Object.entries(params)
        .filter((entry): entry is [string, string | number] => entry[1] !== undefined)
        .map(([key, value]) => [key, String(value)]),
    );
    return (await this.json(await this.request(`/console/ops/ledger?${query}`))) as {
      rows: LedgerRow[];
    };
  }

  async getPacks(): Promise<{
    packs: PackRow[];
    adapter_health?: { status: string; owner: string } | import("./types").AdapterHealthRow[];
    user_roles?: Record<string, unknown>;
  }> {
    return (await this.json(await this.request("/console/ops/packs"))) as {
      packs: PackRow[];
      adapter_health?: { status: string; owner: string } | import("./types").AdapterHealthRow[];
      user_roles?: Record<string, unknown>;
    };
  }

  async getProjectionRpa(claimId: string, projectionId: string) {
    return (await this.json(await this.request(
      `/console/claims/${encodeURIComponent(claimId)}/projections/`
      + `${encodeURIComponent(projectionId)}/rpa`,
    ))) as import("./types").ProjectionRpaView;
  }

  async getProjectionEvidence(url: string): Promise<ArrayBuffer> {
    return (await this.request(url)).arrayBuffer();
  }

  async capturePasteReadback(reviewId: string, observed: Record<string, string>) {
    return (await this.json(await this.request(
      `/console/reviews/${encodeURIComponent(reviewId)}/paste-readback/capture`,
      { method: "POST", body: stringifyLossless({ observed }) },
    ))) as import("./types").PasteReadbackCapture;
  }

  async clearProjectionCircuit(operationId: string): Promise<Record<string, unknown>> {
    return (await this.json(await this.request(
      `/console/ops/projection-circuits/${encodeURIComponent(operationId)}/clear`,
      { method: "POST" },
    ))) as Record<string, unknown>;
  }

  async getCapabilities(): Promise<{ capabilities: CapabilityRow[] }> {
    return (await this.json(await this.request("/console/ops/capabilities"))) as {
      capabilities: CapabilityRow[];
    };
  }

  async promoteCapability(
    id: string,
    body: { to_level: number | string; sign_offs: Array<Record<string, string>> },
  ): Promise<Record<string, unknown>> {
    return (await this.json(await this.request(
      `/console/ops/capabilities/${encodeURIComponent(id)}/promote`,
      { method: "POST", body: stringifyLossless(body) },
    ))) as Record<string, unknown>;
  }

  async listNotifications(): Promise<{ items: Array<Record<string, unknown>> }> {
    return (await this.json(await this.request(
      "/console/ops/notifications?scope=mine",
    ))) as { items: Array<Record<string, unknown>> };
  }

  async markNotificationRead(id: string): Promise<Record<string, unknown>> {
    return (await this.json(await this.request(
      `/console/ops/notifications/${encodeURIComponent(id)}/read`,
      { method: "POST" },
    ))) as Record<string, unknown>;
  }
}
