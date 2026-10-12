import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(path.resolve(__dirname, "index.css"), "utf8");
const shellSource = readFileSync(path.resolve(__dirname, "app/Shell.tsx"), "utf8");

/** The declarations of the first rule whose selector (whitespace-normalised) equals `selector`. */
function ruleBody(css: string, selector: string): string {
  const pattern = new RegExp(
    `(?:^|\\n|\\})\\s*${selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`,
  );
  return pattern.exec(css)?.[1] ?? "";
}

describe("design tokens in index.css", () => {
  it("sets interface body text to the 13px token and leaves the root font size at the browser default", () => {
    expect(stylesheet).toMatch(/--text-sm:\s*13px/);
    expect(ruleBody(stylesheet, "body")).toMatch(/text-sm/);
    expect(ruleBody(stylesheet, "html")).not.toMatch(/font-size/);
    expect(stylesheet).not.toMatch(/--spacing\s*:/);
  });

  it("turns the sidebar into a sheet below 900px", () => {
    expect(stylesheet).toMatch(/@theme\s*\{[^}]*--breakpoint-nav:\s*900px/);
    expect(shellSource).toContain("nav:flex");
    expect(shellSource).toContain("nav:hidden");
    expect(shellSource).not.toMatch(/\bmd:/);
  });

  it("draws every border in the hairline colour unless a component overrides it", () => {
    expect(stylesheet).toMatch(
      /@layer base\s*\{[^@]*\*,\s*::before,\s*::after\s*\{\s*border-color:\s*var\(--border\)/,
    );
  });

  it("stops every animation, pseudo-elements included, under prefers-reduced-motion", () => {
    const block = stylesheet.slice(stylesheet.indexOf("prefers-reduced-motion"));
    expect(block).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{/);
    expect(block).toMatch(/animation:\s*none/);
    expect(block).toMatch(/transition:\s*none/);
  });
});
