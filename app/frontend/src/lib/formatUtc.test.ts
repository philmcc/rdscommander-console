import { describe, expect, it } from "vitest";
import { formatUtc } from "./formatUtc";

describe("formatUtc", () => {
  it("writes a timestamp as UTC to the minute whatever offset it carries", () => {
    expect(formatUtc("2026-10-20T02:00:00Z")).toBe("2026-10-20 02:00 UTC");
    expect(formatUtc("2026-10-20T03:30:45+01:00")).toBe("2026-10-20 02:30 UTC");
  });

  it("returns what is not a date as it is", () => {
    expect(formatUtc("next window")).toBe("next window");
  });
});
