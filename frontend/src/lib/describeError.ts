import { ApiError } from "@/api/client";

/**
 * A sentence fragment saying why a request failed. The backend's `detail` when it gave one; otherwise the
 * status code, because a response over HTTP/2 can arrive with no status text at all.
 *
 * @example
 * describeError(new ApiError(500, "")); // "the server answered 500"
 */
export function describeError(caught: unknown): string {
  if (caught instanceof ApiError) return caught.detail || `the server answered ${caught.status}`;
  if (caught instanceof Error) return caught.message || "the request failed";
  return String(caught);
}
