import { describe, expect, it } from "vitest";
import { backupSummary } from "./backupSummary";

describe("backupSummary", () => {
  it("says days in the plural except for one", () => {
    expect(backupSummary(7, "03:00-04:00")).toBe("7 days, window 03:00-04:00");
    expect(backupSummary(1, "03:00-04:00")).toBe("1 day, window 03:00-04:00");
    expect(backupSummary(0, "03:00-04:00")).toBe("0 days, window 03:00-04:00");
  });

  it("shows a dash for an unknown window and nothing for an unknown retention", () => {
    expect(backupSummary(7, null)).toBe("7 days, window —");
    expect(backupSummary(null, "03:00-04:00")).toBeNull();
  });
});
