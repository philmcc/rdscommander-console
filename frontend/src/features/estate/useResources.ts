import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { ResourceList, ResourceRecord } from "@/api/types";
import { describeError } from "@/lib/describeError";

/**
 * Resources of one kind in an environment, from the latest inventory snapshot.
 *
 * - `resources`: empty until the first answer for this environment and kind; switching either drops the
 *   previous list at once, and an answer that arrives after the switch is ignored.
 * - `loading`: a request is in flight.
 * - `error`: why the latest load failed, `null` when it worked. A failed reload keeps the rows already shown.
 * - `reload`: reads the list now.
 *
 * @example
 * const { resources, loading, error, reload } = useResources(selected?.slug, "instance");
 */
export function useResources(environmentSlug: string | undefined, kind: string) {
  const [resources, setResources] = useState<ResourceRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The list this hook is showing now; an answer for any other is stale.
  const activeList = useRef<string | undefined>(undefined);

  const reload = useCallback(async () => {
    if (!environmentSlug) return;
    const requested = `${environmentSlug}/${kind}`;
    setLoading(true);
    try {
      const loaded = await apiGet<ResourceList>(`/api/v1/environments/${environmentSlug}/resources`, {
        kind,
      });
      if (activeList.current !== requested) return;
      setResources(loaded.resources);
      setError(null);
    } catch (caught) {
      if (activeList.current !== requested) return;
      setError(describeError(caught));
    } finally {
      if (activeList.current === requested) setLoading(false);
    }
  }, [environmentSlug, kind]);

  useEffect(() => {
    activeList.current = environmentSlug ? `${environmentSlug}/${kind}` : undefined;
    setResources([]);
    setError(null);
    void reload();
    return () => {
      activeList.current = undefined;
    };
  }, [environmentSlug, kind, reload]);

  return { resources, reload, loading, error };
}
