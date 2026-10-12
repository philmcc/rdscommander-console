import { afterEach, describe, expect, it, vi } from "vitest";
import { applyTheme, getThemeSnapshot, readThemePreference, resolvedTheme, subscribeTheme } from "./theme";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
});

describe("theme", () => {
  it("defaults to system and resolves it from prefers-color-scheme", () => {
    expect(readThemePreference()).toBe("system");
    expect(["light", "dark"]).toContain(resolvedTheme("system"));
  });

  it("applies dark by toggling the dark class and remembers the choice", () => {
    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(readThemePreference()).toBe("dark");
    applyTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("tells subscribers when a theme is applied, until they unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeTheme(listener);
    applyTheme("dark");
    expect(listener).toHaveBeenCalledTimes(1);
    expect(getThemeSnapshot()).toEqual({ preference: "dark", resolved: "dark" });
    unsubscribe();
    applyTheme("light");
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("gives the same snapshot object until the preference or the resolved theme changes", () => {
    applyTheme("light");
    const first = getThemeSnapshot();
    expect(getThemeSnapshot()).toBe(first);
    applyTheme("dark");
    expect(getThemeSnapshot()).not.toBe(first);
  });

  it("follows a change of the system theme while the preference is system", () => {
    let systemIsDark = false;
    // jsdom has no matchMedia, so there is nothing to spy on.
    vi.stubGlobal("matchMedia", () => ({
      matches: systemIsDark,
      addEventListener: () => {},
      removeEventListener: () => {},
    }));
    applyTheme("system");
    expect(getThemeSnapshot()).toEqual({ preference: "system", resolved: "light" });
    const listener = vi.fn();
    subscribeTheme(listener);
    systemIsDark = true;
    applyTheme("system");
    expect(listener).toHaveBeenCalled();
    expect(getThemeSnapshot()).toEqual({ preference: "system", resolved: "dark" });
  });

  it("still knows the preference for this page when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage is blocked");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage is blocked");
    });
    applyTheme("dark");
    expect(readThemePreference()).toBe("dark");
    expect(getThemeSnapshot().preference).toBe("dark");
  });
});
