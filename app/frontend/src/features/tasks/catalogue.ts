import { apiGet } from "@/api/client";
import type { CatalogueResponse, DefinitionSummary } from "@/api/types";

let cached: Promise<DefinitionSummary[]> | null = null;

/**
 * Catalogue definitions, fetched once per page load. A failed fetch is not remembered, so the next call
 * tries again.
 *
 * @example
 * const definitions = await loadCatalogue();
 */
export function loadCatalogue(): Promise<DefinitionSummary[]> {
  if (!cached) {
    const request = apiGet<CatalogueResponse>("/api/v1/catalogue").then((response) => response.definitions);
    cached = request;
    request.catch(() => {
      if (cached === request) cached = null;
    });
  }
  return cached;
}

/** Test hook: forget the cached catalogue. */
export function resetCatalogueCache(): void {
  cached = null;
}
