import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { EnvironmentList, EnvironmentOverview } from "@/api/types";
import { describeError } from "@/lib/describeError";

/**
 * Every environment with its counts, last refresh and attention items.
 *
 * - `environments`: `null` until the first answer; a failed reload keeps the list already shown.
 * - `error`: why the latest read failed, `null` when it worked.
 * - `reload`: reads the list now. Of several reads in flight only the latest one is used.
 *
 * @example
 * const { environments, error, reload } = useEnvironmentOverviews();
 */
export function useEnvironmentOverviews() {
  const [environments, setEnvironments] = useState<EnvironmentOverview[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const latestRead = useRef(0);
  const mounted = useRef(false);

  const reload = useCallback(async () => {
    latestRead.current += 1;
    const thisRead = latestRead.current;
    try {
      const answer = await apiGet<EnvironmentList>("/api/v1/environments");
      if (!mounted.current || thisRead !== latestRead.current) return;
      setEnvironments(answer.environments);
      setError(null);
    } catch (caught) {
      if (!mounted.current || thisRead !== latestRead.current) return;
      setError(describeError(caught));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void reload();
    return () => {
      mounted.current = false;
    };
  }, [reload]);

  return { environments, error, reload };
}
