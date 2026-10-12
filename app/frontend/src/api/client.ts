import type { Bootstrap } from "./types";

/**
 * Error from the backend; `problems` is present on selection-validation failures. A refusal that names its
 * cause carries `diagnosticKind` (`approval_stage_mismatch`…) and the `fixSuggestion` the server gave, from
 * an object `detail` or from beside a text one; both are null otherwise.
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
    public readonly problems: string[] = [],
    public readonly diagnosticKind: string | null = null,
    public readonly fixSuggestion: string | null = null,
  ) {
    super(detail);
  }
}

/**
 * The body of `POST /api/v1/operations/{id}/approve`, written by hand until the generated contract has it.
 * `approval_stage` is the pause being approved (`plan.approval_stage`: a major upgrade's `create_green` or
 * `switchover`; null for an operation that pauses once). `confirmation_text` is what the operator typed to
 * confirm the identifier, or null when the operation asks for none. The server refuses (409) an approval
 * whose stage is not the one the operation waits at, or whose text is not the identifier.
 *
 * @example
 * const body: OperationApproveRequest = { approval_stage: "switchover", confirmation_text: "demo-orders" };
 */
export interface OperationApproveRequest {
  approval_stage: string | null;
  confirmation_text: string | null;
}

let bootstrapPromise: Promise<Bootstrap> | null = null;

async function fetchBootstrap(): Promise<Bootstrap> {
  const response = await fetch("/api/v1/bootstrap");
  if (!response.ok) throw new ApiError(response.status, "console backend is not reachable");
  return (await response.json()) as Bootstrap;
}

/** Fetch `/api/v1/bootstrap` once per page load; later calls reuse the result. */
export function loadBootstrap(): Promise<Bootstrap> {
  bootstrapPromise ??= fetchBootstrap();
  return bootstrapPromise;
}

/**
 * Fetch `/api/v1/bootstrap` again, for when the configuration changed (an environment was imported). The
 * answer replaces the one `loadBootstrap` returns; a failed fetch leaves the earlier answer in place.
 *
 * @example
 * const fresh = await reloadBootstrap();
 */
export async function reloadBootstrap(): Promise<Bootstrap> {
  const fresh = await fetchBootstrap();
  bootstrapPromise = Promise.resolve(fresh);
  return fresh;
}

/** Test hook: forget the cached bootstrap. */
export function resetBootstrapCache(): void {
  bootstrapPromise = null;
}

/** Where a validation error happened, which says nothing about the field: the request part it was in. */
const REQUEST_LOCATIONS = new Set(["path", "query", "body"]);

/**
 * One validation error, or any other value the server put in `detail` or `problems`, as text. A
 * FastAPI-style `{loc, msg}` reads `<field path>: <message>`, with the request part left out of the path;
 * an object with only a `msg` or `message` reads as that message.
 *
 * @example
 * problemText({ loc: ["path", "operation_id"], msg: "Input should be a valid UUID" });
 * // "operation_id: Input should be a valid UUID"
 */
function problemText(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const { loc, msg, message } = value as Record<string, unknown>;
    const text = typeof msg === "string" ? msg : typeof message === "string" ? message : null;
    if (text !== null) {
      const path = Array.isArray(loc)
        ? loc.map(String).filter((part, index) => index > 0 || !REQUEST_LOCATIONS.has(part))
        : [];
      return path.length > 0 ? `${path.join(" -> ")}: ${text}` : text;
    }
  }
  return JSON.stringify(value);
}

/** A non-empty text field of an object, or null. */
function textField(source: Record<string, unknown>, field: string): string | null {
  const value = source[field];
  return typeof value === "string" && value !== "" ? value : null;
}

/** An object detail that names its cause: `{diagnostic_kind or kind, detail or message, fix_suggestion}`. */
function diagnosticDetailOf(
  value: unknown,
): { text: string | null; diagnosticKind: string | null; fixSuggestion: string | null } | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const fields = value as Record<string, unknown>;
  const diagnosticKind = textField(fields, "diagnostic_kind") ?? textField(fields, "kind");
  const fixSuggestion = textField(fields, "fix_suggestion");
  if (diagnosticKind === null && fixSuggestion === null) return null;
  const text =
    textField(fields, "detail") ??
    textField(fields, "message") ??
    textField(fields, "msg") ??
    textField(fields, "title");
  return { text, diagnosticKind, fixSuggestion };
}

/**
 * Why a request failed, from the error body: `detail` as one sentence and `problems` as text, whatever shape
 * the server sent. A list-valued `detail` (a validation error FastAPI answered itself) becomes "the request
 * is not valid: <problem>; <problem>" and its entries become the problems; an object becomes its message.
 * A refusal that names its cause keeps its diagnostic kind and fix suggestion, whether they are in an object
 * `detail` or beside a text one. A body that is not JSON leaves the status text.
 */
async function parseError(response: Response): Promise<ApiError> {
  let detail = response.statusText;
  let problems: string[] = [];
  let diagnosticKind: string | null = null;
  let fixSuggestion: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (Array.isArray(body.problems)) problems = body.problems.map(problemText);
    diagnosticKind = textField(body, "diagnostic_kind");
    fixSuggestion = textField(body, "fix_suggestion");
    const diagnosticDetail = diagnosticDetailOf(body.detail);
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body.detail)) {
      const listed = body.detail.map(problemText);
      detail = `the request is not valid: ${listed.join("; ")}`;
      if (problems.length === 0) problems = listed;
    } else if (diagnosticDetail !== null) {
      detail = diagnosticDetail.text ?? detail;
      diagnosticKind = diagnosticDetail.diagnosticKind ?? diagnosticKind;
      fixSuggestion = diagnosticDetail.fixSuggestion ?? fixSuggestion;
    } else if (body.detail !== undefined && body.detail !== null) {
      detail = problemText(body.detail);
    }
  } catch {
    // body was not JSON
  }
  return new ApiError(response.status, detail, problems, diagnosticKind, fixSuggestion);
}

/** GET with optional query parameters; nullish or empty values are omitted. */
export async function apiGet<T>(
  path: string,
  query?: Record<string, string | number | null | undefined>,
): Promise<T> {
  const parameters = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== null && value !== undefined && value !== "") parameters.set(key, String(value));
  }
  const suffix = parameters.toString();
  const response = await fetch(suffix ? `${path}?${suffix}` : path);
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

/** POST JSON with the CSRF token from bootstrap. */
export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const { csrf_token } = await loadBootstrap();
  const response = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json", "X-CSRF-Token": csrf_token },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}
