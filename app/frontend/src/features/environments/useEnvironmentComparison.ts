import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";
import type { EnvironmentComparison } from "@/api/types";
import { describeError } from "@/lib/describeError";

/** What was asked of the comparison. */
export interface ComparisonRequest {
  left: string;
  right: string;
  /** Comma-separated suffixes or prefixes to strip before matching identifiers; empty for exact matches only. */
  strip: string;
  /** Changes each time the operator asks again for the same comparison, so it is read again. */
  askedAt: string;
}

interface Settled {
  requestKey: string;
  comparison: EnvironmentComparison | null;
  error: string | null;
}

/**
 * The comparison of two environments. Nothing is read while `request` is `null` or names the same
 * environment twice.
 *
 * - `comparison`: the answer to the current left, right and strip list; `null` while it is read and when
 *   the request changed, so an answer is never shown under another request. Asking again for the same
 *   comparison keeps the answer until the new one arrives.
 * - `error`: why the read failed, `null` when it worked.
 * - `busy`: a read is in flight.
 *
 * @example
 * const { comparison, error, busy } = useEnvironmentComparison({ left: "staging", right: "production", strip: "-prod", askedAt: location.key });
 */
export function useEnvironmentComparison(request: ComparisonRequest | null) {
  const readable =
    request !== null && request.left !== "" && request.right !== "" && request.left !== request.right;
  const requestKey = readable ? JSON.stringify([request.left, request.right, request.strip]) : null;
  const askedAt = request?.askedAt ?? "";
  const [settled, setSettled] = useState<Settled | null>(null);
  const [reading, setReading] = useState<{ requestKey: string; askedAt: string } | null>(null);

  useEffect(() => {
    if (requestKey === null) return;
    const [left, right, strip] = JSON.parse(requestKey) as [string, string, string];
    let cancelled = false;
    setReading({ requestKey, askedAt });
    apiGet<EnvironmentComparison>("/api/v1/environments/compare", { left, right, strip }).then(
      (comparison) => {
        if (cancelled) return;
        setSettled({ requestKey, comparison, error: null });
        setReading(null);
      },
      (caught: unknown) => {
        if (cancelled) return;
        setSettled({ requestKey, comparison: null, error: describeError(caught) });
        setReading(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [requestKey, askedAt]);

  const current = settled !== null && settled.requestKey === requestKey ? settled : null;
  return {
    comparison: current?.comparison ?? null,
    error: current?.error ?? null,
    busy: requestKey !== null && reading?.requestKey === requestKey,
  };
}
