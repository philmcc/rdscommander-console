import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { OperationList, OperationSummary } from "@/api/types";
import { describeError } from "@/lib/describeError";

/** How often the list is read again, so running operations move along and new ones appear. */
const RELOAD_INTERVAL_MILLISECONDS = 5000;
/** The most operations one answer carries. */
const LIST_LIMIT = 100;

/**
 * Passed as the environment to list every environment's operations and the ones that belong to none (an
 * environment import), instead of one environment's.
 */
export const ALL_ENVIRONMENTS = null;

/** Names one list: its environment (or every one), states and kind; an answer for another list is stale. */
function listKeyOf(
  environmentSlug: string | null | undefined,
  states: string,
  kind: string,
): string | undefined {
  if (environmentSlug === undefined) return undefined;
  return JSON.stringify([environmentSlug, states, kind]);
}

/**
 * The latest operations of an environment, or of every environment, read again every five seconds.
 *
 * `environmentSlug` is the environment to list; `ALL_ENVIRONMENTS` lists every environment's operations and
 * those of none; `undefined` (no environment chosen yet) reads nothing. `states` is the server-side state
 * filter as the API takes it, comma separated (`"FAILED"`, `"QUEUED,RUNNING,AWAITING_APPROVAL"`), or empty
 * for every state. `kind` keeps one operation kind, such as `environment_import`; empty for every kind.
 *
 * - `operations`: empty until the first answer for this list; changing the environment, states or kind
 *   drops the previous rows at once, and an answer that arrives after the change is ignored.
 * - `loading`: no answer has arrived yet for this list.
 * - `error`: why the latest load failed, `null` when it worked. A failed reload keeps the rows already shown.
 * - `reload`: reads the list now.
 *
 * @example
 * const { operations, loading, error, reload } = useOperations(selected?.slug, "FAILED");
 * const imports = useOperations(ALL_ENVIRONMENTS, "", "environment_import");
 */
export function useOperations(environmentSlug: string | null | undefined, states: string, kind = "") {
  const [operations, setOperations] = useState<OperationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The list this hook is showing now; an answer for any other is stale.
  const activeList = useRef<string | undefined>(undefined);

  const reload = useCallback(async () => {
    const requested = listKeyOf(environmentSlug, states, kind);
    if (requested === undefined) return;
    try {
      const loaded = await apiGet<OperationList>("/api/v1/operations", {
        environment_slug: environmentSlug,
        kind,
        state: states,
        limit: LIST_LIMIT,
      });
      if (activeList.current !== requested) return;
      setOperations(loaded.operations);
      setError(null);
    } catch (caught) {
      if (activeList.current !== requested) return;
      setError(describeError(caught));
    } finally {
      if (activeList.current === requested) setLoading(false);
    }
  }, [environmentSlug, states, kind]);

  useEffect(() => {
    activeList.current = listKeyOf(environmentSlug, states, kind);
    setOperations([]);
    setLoading(true);
    setError(null);
    void reload();
    const timer = setInterval(() => void reload(), RELOAD_INTERVAL_MILLISECONDS);
    return () => {
      activeList.current = undefined;
      clearInterval(timer);
    };
  }, [environmentSlug, states, kind, reload]);

  return { operations, loading, error, reload };
}
