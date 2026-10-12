import { useEffect, useRef, useState } from "react";
import { describeError } from "@/lib/describeError";

interface Answer<T> {
  key: string;
  data: T | null;
  error: string | null;
}

/**
 * A read-only answer that belongs to `key`, such as the list of snapshots next to a resource.
 *
 * `load` runs once for each `key`; a new key drops the previous answer at once and ignores it if it is
 * still on its way. `data` and `error` are both `null` until the answer for the current key arrives.
 *
 * @example
 * const { data, error } = useRelatedData(`${slug}/${snapshotId}`, () => apiGet<ResourceList>(path));
 */
export function useRelatedData<T>(
  key: string,
  load: () => Promise<T>,
): { data: T | null; error: string | null } {
  const [answer, setAnswer] = useState<Answer<T> | null>(null);
  // The caller builds `load` on every render; the effect runs it for the key it started with.
  const latestLoad = useRef(load);
  useEffect(() => {
    latestLoad.current = load;
  });
  useEffect(() => {
    let cancelled = false;
    latestLoad.current().then(
      (data) => {
        if (!cancelled) setAnswer({ key, data, error: null });
      },
      (caught: unknown) => {
        if (!cancelled) setAnswer({ key, data: null, error: describeError(caught) });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [key]);
  const current = answer?.key === key ? answer : null;
  return { data: current?.data ?? null, error: current?.error ?? null };
}
