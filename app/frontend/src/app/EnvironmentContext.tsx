import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { loadBootstrap, reloadBootstrap } from "@/api/client";
import type { Bootstrap, EnvironmentSummary } from "@/api/types";

const SELECTED_ENVIRONMENT_KEY = "rdscommander.selectedEnvironment";

interface EnvironmentState {
  bootstrap: Bootstrap | null;
  error: string | null;
  environments: EnvironmentSummary[];
  selected: EnvironmentSummary | null;
  select: (slug: string) => void;
  /**
   * Reads bootstrap from the server again so a changed configuration (a new environment) shows up. Rejects
   * when the backend cannot be reached; the environments already held stay in place.
   */
  reload: () => Promise<void>;
}

const EnvironmentContext = createContext<EnvironmentState | null>(null);

function remembered(): string | null {
  try {
    return window.localStorage.getItem(SELECTED_ENVIRONMENT_KEY);
  } catch {
    return null;
  }
}

/** Loads bootstrap once and holds the selected environment for every page. */
export function EnvironmentProvider({ children }: { children: ReactNode }) {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [slug, setSlug] = useState<string>("");

  const apply = useCallback((loaded: Bootstrap) => {
    setBootstrap(loaded);
    setError(null);
    const stored = remembered();
    const first = loaded.environments[0]?.slug ?? "";
    setSlug(
      (current) => current || (stored && loaded.environments.some((e) => e.slug === stored) ? stored : first),
    );
  }, []);
  useEffect(() => {
    loadBootstrap().then(apply, (caught: unknown) => setError((caught as Error).message));
  }, [apply]);
  const reload = useCallback(async () => apply(await reloadBootstrap()), [apply]);

  const select = useCallback((next: string) => {
    setSlug(next);
    try {
      window.localStorage.setItem(SELECTED_ENVIRONMENT_KEY, next);
    } catch {
      // storage unavailable
    }
  }, []);

  const value = useMemo<EnvironmentState>(() => {
    const environments = bootstrap?.environments ?? [];
    return {
      bootstrap,
      error,
      environments,
      selected: environments.find((e) => e.slug === slug) ?? null,
      select,
      reload,
    };
  }, [bootstrap, error, slug, select, reload]);
  return <EnvironmentContext.Provider value={value}>{children}</EnvironmentContext.Provider>;
}

/** The selected environment and the list; throws outside the provider. */
export function useEnvironment(): EnvironmentState {
  const value = useContext(EnvironmentContext);
  if (!value) throw new Error("useEnvironment must be used inside EnvironmentProvider");
  return value;
}

/** Hue index for an environment, by configuration order. */
export function environmentHue(environments: EnvironmentSummary[], slug: string): number {
  const index = environments.findIndex((e) => e.slug === slug);
  return (index < 0 ? 0 : index) % 8;
}
