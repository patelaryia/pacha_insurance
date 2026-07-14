import { afterEach, describe, expect, it, vi } from "vitest";

import { ConsoleApiClient } from "./client";

afterEach(() => vi.unstubAllGlobals());

function client() {
  return new ConsoleApiClient({ baseUrl: "https://api.test", getAccessToken: async () => "token" });
}

describe("lossless console transport", () => {
  it("keeps nested review integers as bigint and exposes auth errors", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('{"items":[{"id":"r1","claim_id":null,"type":"FIELD_VERIFY","subtype":null,"status":"open","assigned_to":null,"payload":{"amount":900719925474099312345},"workspace_layout":"field_verify","resolution_schema":"FIELD_VERIFY@1","sla":[]}]}'))
      .mockResolvedValueOnce(new Response('{"code":"INVALID_TOKEN","detail":"Expired"}', { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);

    const [item] = await client().listReviews({ scope: "mine" });
    expect(item.payload.amount).toBe(900719925474099312345n);
    await expect(client().listReviews({ scope: "mine" })).rejects.toEqual({
      code: "INVALID_TOKEN",
      detail: "Expired",
    });
  });

  it("normalises only structural numbers and serialises bigint corrections exactly", async () => {
    const claimBody = '{"claim":{"id":"c1","status":"RESERVED","substatus":null,"assigned_to":null,"created_at":"2026-01-01T00:00:00Z","updated_at":"2026-01-01T00:00:00Z"},"header":{"insured":null,"registration":null,"amount_cents":"900719925474099312345"},"fields":[{"path":"reserve.total","value":900719925474099312345,"value_type":"money","verification_state":"extracted","confidence":1,"source_type":"extraction","has_citation":true}],"documents":[],"financials":[{"path":"reserve.total","amount_cents":"900719925474099312345","calc_run_id":"calc-1"}],"timeline":[],"systems":[],"communications":[],"availability":{}}';
    const citationBody = '{"claim_id":"c1","field_path":"reserve.total","value":"900719925474099312345","value_type":"money","verification_state":"extracted","document_id":"d1","page":1,"bbox":[0,0.25,1,0.75],"document_url":"/d1.pdf"}';
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(claimBody))
      .mockResolvedValueOnce(new Response(citationBody))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(new Uint8Array([1, 2, 3])));
    vi.stubGlobal("fetch", fetchMock);
    const api = client();

    const claim = await api.getClaim360("c1");
    expect(claim.header.amount_cents).toBe(900719925474099312345n);
    expect(claim.fields[0].value).toBe(900719925474099312345n);
    expect(claim.fields[0].confidence).toBe(1);
    expect(claim.financials[0].amount_cents).toBe(900719925474099312345n);
    const citation = await api.getCitation("c1", "reserve.total");
    expect(citation).toMatchObject({ value: 900719925474099312345n, page: 1, bbox: [0, 0.25, 1, 0.75] });

    await api.resolveReview("r1", {
      action: "edit_approve",
      schema_version: "FIELD_VERIFY@1",
      payload: { capability_id: "reserve.calculate", corrected_fields: { "reserve.total": 900719925474099312345n } },
    });
    expect(fetchMock.mock.calls[2][1]?.body).toContain("900719925474099312345");
    expect(await api.getDocument("/d1.pdf")).toBeInstanceOf(ArrayBuffer);
  });

  it("falls back to the HTTP status for non-JSON failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("gateway down", { status: 503 })));
    await expect(client().listReviews({ scope: "pool" })).rejects.toEqual({
      code: "HTTP_503",
      detail: "Request failed",
    });
  });
});
