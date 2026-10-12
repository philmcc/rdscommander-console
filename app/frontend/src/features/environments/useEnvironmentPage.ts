import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { EnvironmentPageData } from "@/api/types";
import { describeError } from "@/lib/describeError";

interface LoadedPage {
  slug: string;
  page: EnvironmentPageData;
}

interface PageFailure {
  slug: string;
  message: string;
}

/**
 * One environment's page, reloadable after a task finishes.
 *
 * - `page`: the page of `slug`; `null` until the first answer, and again as soon as `slug` changes, so a page
 *   is never shown under the address of another environment. A reload keeps the page it has until the new
 *   answer arrives.
 * - `error`: why the latest read failed, `null` when it worked. A failed reload keeps the page already shown.
 * - `reload`: reads the page now. An answer that arrives after `slug` changed is ignored.
 *
 * @example
 * const { page, reload, error } = useEnvironmentPage(slug);
 */
export function useEnvironmentPage(slug: string | undefined) {
  const [loaded, setLoaded] = useState<LoadedPage | null>(null);
  const [failure, setFailure] = useState<PageFailure | null>(null);
  // The environment this hook is showing now; an answer for any other is stale.
  const activeSlug = useRef<string | undefined>(undefined);

  const reload = useCallback(async () => {
    if (!slug) return;
    try {
      const page = await apiGet<EnvironmentPageData>(`/api/v1/environments/${encodeURIComponent(slug)}`);
      if (activeSlug.current !== slug) return;
      setLoaded({ slug, page });
      setFailure(null);
    } catch (caught) {
      if (activeSlug.current !== slug) return;
      setFailure({ slug, message: describeError(caught) });
    }
  }, [slug]);

  useEffect(() => {
    activeSlug.current = slug;
    void reload();
    return () => {
      activeSlug.current = undefined;
    };
  }, [slug, reload]);

  return {
    page: loaded && loaded.slug === slug ? loaded.page : null,
    error: failure && failure.slug === slug ? failure.message : null,
    reload,
  };
}
