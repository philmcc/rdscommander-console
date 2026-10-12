import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, apiGet } from "@/api/client";
import type { ResourcePage } from "@/api/types";
import { describeError } from "@/lib/describeError";

interface LoadedPage {
  resourceId: string;
  page: ResourcePage;
}

interface PageFailure {
  resourceId: string;
  message: string;
  /** The console answered 404: the resource is not in the latest inventory (deleted, or never there). */
  gone: boolean;
}

/**
 * One resource page, reloadable after an operation finishes.
 *
 * - `page`: the page of `resourceId`; `null` until the first answer, and again as soon as `resourceId`
 *   changes, so a page is never shown under the address of another resource. A reload keeps the page it
 *   has until the new answer arrives.
 * - `error`: why the latest read failed, `null` when it worked. A failed reload keeps the page already shown,
 *   except when the resource is gone from the inventory (a 404, such as after a delete): then the page is
 *   dropped, so a deleted resource is never shown as it was.
 * - `gone`: the latest read found the resource missing from the inventory.
 * - `reload`: reads the page now. An answer that arrives after `resourceId` changed is ignored.
 *
 * @example
 * const { page, reload, error } = useResourcePage(resourceId);
 */
export function useResourcePage(resourceId: string | undefined) {
  const [loaded, setLoaded] = useState<LoadedPage | null>(null);
  const [failure, setFailure] = useState<PageFailure | null>(null);
  // The resource this hook is showing now; an answer for any other is stale.
  const activeResourceId = useRef<string | undefined>(undefined);

  const reload = useCallback(async () => {
    if (!resourceId) return;
    try {
      const page = await apiGet<ResourcePage>(`/api/v1/resources/${resourceId}`);
      if (activeResourceId.current !== resourceId) return;
      setLoaded({ resourceId, page });
      setFailure(null);
    } catch (caught) {
      if (activeResourceId.current !== resourceId) return;
      const gone = caught instanceof ApiError && caught.status === 404;
      if (gone) setLoaded(null);
      setFailure({ resourceId, message: describeError(caught), gone });
    }
  }, [resourceId]);

  useEffect(() => {
    activeResourceId.current = resourceId;
    void reload();
    return () => {
      activeResourceId.current = undefined;
    };
  }, [resourceId, reload]);

  return {
    page: loaded && loaded.resourceId === resourceId ? loaded.page : null,
    error: failure && failure.resourceId === resourceId ? failure.message : null,
    gone: Boolean(failure && failure.resourceId === resourceId && failure.gone),
    reload,
  };
}
