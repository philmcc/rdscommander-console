import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { AttentionInbox } from "@/api/types";
import { describeError } from "@/lib/describeError";

/** How often the inbox is read again while a page shows it. */
const RELOAD_INTERVAL_MILLISECONDS = 15_000;

/**
 * The inbox for an environment, reloaded every 15 seconds.
 *
 * - `inbox`: `null` until the first answer for this environment; switching environment drops the previous
 *   environment's rows at once, and an answer that arrives after the switch is ignored.
 * - `error`: why the latest load failed, `null` when it worked. A failed reload keeps the rows already shown.
 * - `reload`: reads the inbox now.
 *
 * @example
 * const { inbox, error, reload } = useAttention(selected?.slug);
 */
export function useAttention(environmentSlug: string | undefined) {
  const [inbox, setInbox] = useState<AttentionInbox | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The environment this hook is showing now; a load for any other is stale.
  const activeSlug = useRef<string | undefined>(undefined);

  const reload = useCallback(async () => {
    if (!environmentSlug) return;
    try {
      const loaded = await apiGet<AttentionInbox>("/api/v1/attention", { environment_slug: environmentSlug });
      if (activeSlug.current !== environmentSlug) return;
      setInbox(loaded);
      setError(null);
    } catch (caught) {
      if (activeSlug.current !== environmentSlug) return;
      setError(describeError(caught));
    }
  }, [environmentSlug]);

  useEffect(() => {
    activeSlug.current = environmentSlug;
    setInbox(null);
    setError(null);
    void reload();
    const timer = setInterval(() => void reload(), RELOAD_INTERVAL_MILLISECONDS);
    return () => {
      activeSlug.current = undefined;
      clearInterval(timer);
    };
  }, [environmentSlug, reload]);

  return { inbox, error, reload };
}
