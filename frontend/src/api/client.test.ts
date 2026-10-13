import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type ApiError,
  apiGet,
  apiPost,
  loadBootstrap,
  reloadBootstrap,
  resetBootstrapCache,
} from "./client";

const bootstrapBody = {
  csrf_token: "token-123",
  operator_name: "phil",
  console_version: "0.1.0",
  environments: [],
};

function mockFetch(responses: Array<{ status: number; body: unknown }>) {
  const queue = [...responses];
  return vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
    const next = queue.shift();
    if (!next) throw new Error("unexpected fetch");
    return new Response(JSON.stringify(next.body), {
      status: next.status,
      headers: { "content-type": "application/json" },
    });
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
});

describe("api client", () => {
  it("loads bootstrap once and sends the csrf token on posts", async () => {
    const fetchMock = mockFetch([
      { status: 200, body: bootstrapBody },
      { status: 202, body: { operation_id: "op", state: "QUEUED" } },
    ]);
    vi.stubGlobal("fetch", fetchMock);
    await loadBootstrap();
    await loadBootstrap();
    await apiPost("/api/v1/environments/sandbox/inventory/refresh", { idempotency_key: "k" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("token-123");
  });

  it("turns 422 bodies with problems into ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        { status: 200, body: bootstrapBody },
        { status: 422, body: { detail: "selection cannot be adopted", problems: ["x needs y"] } },
      ]),
    );
    await expect(apiPost("/api/v1/terraform/adoptions", {})).rejects.toMatchObject({
      status: 422,
      problems: ["x needs y"],
    } satisfies Partial<ApiError>);
  });

  it("turns a list-valued 422 detail into a sentence and its messages into problems", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        {
          status: 422,
          body: {
            detail: [
              {
                type: "uuid_parsing",
                loc: ["path", "operation_id"],
                msg: "Input should be a valid UUID",
                input: "not-a-uuid",
              },
              { type: "greater_than_equal", loc: ["query", "limit"], msg: "Input should be at least 1" },
            ],
          },
        },
      ]),
    );
    await expect(apiGet("/api/v1/operations/not-a-uuid")).rejects.toMatchObject({
      status: 422,
      detail:
        "the request is not valid: operation_id: Input should be a valid UUID; limit: Input should be at least 1",
      problems: ["operation_id: Input should be a valid UUID", "limit: Input should be at least 1"],
    } satisfies Partial<ApiError>);
  });

  it("turns an object detail into its message, and never hands on a problem that is not text", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        {
          status: 400,
          body: { detail: { msg: "the plan has expired" }, problems: [{ msg: "plan 7 expired" }] },
        },
      ]),
    );
    await expect(apiGet("/api/v1/terraform/runs/7/plan")).rejects.toMatchObject({
      detail: "the plan has expired",
      problems: ["plan 7 expired"],
    } satisfies Partial<ApiError>);
  });

  it("keeps the diagnostic kind and fix suggestion of a refusal whose detail is an object", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        { status: 200, body: bootstrapBody },
        {
          status: 409,
          body: {
            detail: {
              diagnostic_kind: "approval_stage_mismatch",
              detail: "the operation waits at switchover, not create_green",
              fix_suggestion: "review the switchover and approve it",
            },
          },
        },
      ]),
    );
    await expect(apiPost("/api/v1/operations/op-1/approve", {})).rejects.toMatchObject({
      status: 409,
      detail: "the operation waits at switchover, not create_green",
      diagnosticKind: "approval_stage_mismatch",
      fixSuggestion: "review the switchover and approve it",
    } satisfies Partial<ApiError>);
  });

  it("keeps the diagnostic kind and fix suggestion of a refusal that puts them beside a text detail", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        { status: 200, body: bootstrapBody },
        {
          status: 409,
          body: {
            detail: "the typed text does not match the identifier shown on the review",
            diagnostic_kind: "confirmation_text_mismatch",
            fix_suggestion: "type the identifier shown on the review exactly",
          },
        },
      ]),
    );
    await expect(apiPost("/api/v1/operations/op-1/approve", {})).rejects.toMatchObject({
      detail: "the typed text does not match the identifier shown on the review",
      diagnosticKind: "confirmation_text_mismatch",
      fixSuggestion: "type the identifier shown on the review exactly",
    } satisfies Partial<ApiError>);
  });

  it("encodes query parameters on get", async () => {
    const fetchMock = mockFetch([{ status: 200, body: { records: [] } }]);
    vi.stubGlobal("fetch", fetchMock);
    await apiGet("/api/v1/history", { record_kind: "aws_api", text: "Describe DB" });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/history?record_kind=aws_api&text=Describe+DB");
  });

  it("reloads bootstrap from the server, and later posts use the token of the fresh answer", async () => {
    const fetchMock = mockFetch([
      { status: 200, body: bootstrapBody },
      { status: 200, body: { ...bootstrapBody, csrf_token: "token-456" } },
      { status: 202, body: { operation_id: "op", state: "QUEUED" } },
    ]);
    vi.stubGlobal("fetch", fetchMock);
    await loadBootstrap();
    const fresh = await reloadBootstrap();
    expect(fresh.csrf_token).toBe("token-456");
    expect(await loadBootstrap()).toBe(fresh);
    await apiPost("/api/v1/environments/sandbox/inventory/refresh", { idempotency_key: "k" });
    const [, init] = fetchMock.mock.calls[2] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("token-456");
  });

  it("keeps the bootstrap it has when a reload fails", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch([
        { status: 200, body: bootstrapBody },
        { status: 503, body: {} },
      ]),
    );
    await loadBootstrap();
    await expect(reloadBootstrap()).rejects.toMatchObject({ status: 503 } satisfies Partial<ApiError>);
    expect((await loadBootstrap()).csrf_token).toBe("token-123");
  });
});
