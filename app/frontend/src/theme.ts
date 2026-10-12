export type ThemePreference = "system" | "light" | "dark";
const THEME_KEY = "rdscommander.theme";

/** What the theme controls show: the preference, and the theme it comes to (`system` follows the OS). */
export interface ThemeSnapshot {
  preference: ThemePreference;
  resolved: "light" | "dark";
}

/** The preference applied while storage is unavailable, so this page still knows what it applied. */
let preferenceWithoutStorage: ThemePreference | null = null;
const themeListeners = new Set<() => void>();
let lastSnapshot: ThemeSnapshot | null = null;

/** Stored preference; `system` when nothing is stored or storage is unavailable. */
export function readThemePreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return preferenceWithoutStorage ?? "system";
  }
}

/** Light or dark for a preference, following prefers-color-scheme for `system`. */
export function resolvedTheme(preference: ThemePreference): "light" | "dark" {
  if (preference !== "system") return preference;
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/**
 * Apply a preference to the document and remember it, then tell the subscribers, so every control that
 * shows the theme (the top-bar toggle, the Settings radios) changes with it.
 */
export function applyTheme(preference: ThemePreference): void {
  document.documentElement.classList.toggle("dark", resolvedTheme(preference) === "dark");
  document.documentElement.dataset.theme = resolvedTheme(preference);
  try {
    window.localStorage.setItem(THEME_KEY, preference);
    preferenceWithoutStorage = null;
  } catch {
    // storage unavailable; the theme still applies for this page
    preferenceWithoutStorage = preference;
  }
  for (const listener of themeListeners) listener();
}

/**
 * Be told whenever `applyTheme` runs. Returns the function that stops the notifications. The pair with
 * `getThemeSnapshot` is what `useSyncExternalStore` takes.
 *
 * @example
 * const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot);
 */
export function subscribeTheme(listener: () => void): () => void {
  themeListeners.add(listener);
  return () => {
    themeListeners.delete(listener);
  };
}

/** The preference and the theme it resolves to now; the same object until either changes. */
export function getThemeSnapshot(): ThemeSnapshot {
  const preference = readThemePreference();
  const resolved = resolvedTheme(preference);
  if (lastSnapshot?.preference !== preference || lastSnapshot.resolved !== resolved) {
    lastSnapshot = { preference, resolved };
  }
  return lastSnapshot;
}

/** Re-apply when the system theme changes and the preference is `system`. */
export function watchSystemTheme(): () => void {
  if (typeof window.matchMedia !== "function") return () => {};
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const listener = () => {
    if (readThemePreference() === "system") applyTheme("system");
  };
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}
