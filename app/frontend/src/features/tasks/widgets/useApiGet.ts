import { useEffect, useState } from "react";
import { apiGet } from "@/api/client";

type Query = Record<string, string | number | null | undefined>;

/** What a widget knows about one GET: nothing yet, the data, or why it failed. */
export interface ApiGetState<T> {
  data: T | null;
  error: string | null;
}

interface Settled<T> extends ApiGetState<T> {
  requestKey: string;
}

/**
 * GET one path for a widget; pass `null` to wait until the inputs are known. Changing the path or query
 * discards the previous answer, and an answer to an outdated request is ignored.
 *
 * @example
 * const profiles = useApiGet<ProfileList>("/api/v1/environments/import/profiles");
 */
export function useApiGet<T>(path: string | null, query?: Query): ApiGetState<T> {
  const queryKey = JSON.stringify(query ?? {});
  const requestKey = path === null ? null : `${path}?${queryKey}`;
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  useEffect(() => {
    if (path === null || requestKey === null) return;
    let cancelled = false;
    apiGet<T>(path, JSON.parse(queryKey) as Query).then(
      (data) => {
        if (!cancelled) setSettled({ requestKey, data, error: null });
      },
      (caught: unknown) => {
        if (!cancelled)
          setSettled({
            requestKey,
            data: null,
            error: caught instanceof Error ? caught.message : String(caught),
          });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [path, queryKey, requestKey]);
  if (settled === null || settled.requestKey !== requestKey) return { data: null, error: null };
  return { data: settled.data, error: settled.error };
}
