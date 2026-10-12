import { describe, expect, it } from "vitest";
import { sortProblems } from "./problems";

const fieldNames = ["name", "target_key", "parameters"];

describe("sortProblems", () => {
  it("puts a field-prefixed message under its field without the prefix", () => {
    expect(sortProblems(["name: String should match pattern"], fieldNames)).toEqual({
      byField: { name: "String should match pattern" },
      general: [],
    });
  });

  it("reads the options prefix and keeps the inner path of a nested error", () => {
    const sorted = sortProblems(
      [
        "options -> target_key: differs from the resource's target",
        "parameters -> work_mem: Input should be a string",
      ],
      fieldNames,
    );
    expect(sorted.byField).toEqual({
      target_key: "differs from the resource's target",
      parameters: "work_mem: Input should be a string",
    });
    expect(sorted.general).toEqual([]);
  });

  it("joins several messages for one field", () => {
    expect(sortProblems(["name: too short", "name: not lowercase"], fieldNames).byField).toEqual({
      name: "too short; not lowercase",
    });
  });

  it("keeps a message that names no field of the form whole, even when it contains a colon", () => {
    const arn = "arn:aws:rds:eu-west-2:000000000000:pg:orders: already adopted";
    const sorted = sortProblems(["work_mem must be an integer", arn, "unknown_field: nope"], fieldNames);
    expect(sorted.byField).toEqual({});
    expect(sorted.general).toEqual(["work_mem must be an integer", arn, "unknown_field: nope"]);
  });

  it("drops the empty location of a whole-form validation error", () => {
    expect(sortProblems([": choose either a profile or the simulated account"], fieldNames)).toEqual({
      byField: {},
      general: ["choose either a profile or the simulated account"],
    });
  });
});
