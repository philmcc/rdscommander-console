import { describe, expect, it } from "vitest";
import { displayValue, names, objects, scalar, textMap } from "./recordValues";

describe("recordValues", () => {
  it("reads scalars and nothing else as a scalar", () => {
    const values = { a: "text", b: 3, c: false, d: ["x"], e: { f: 1 }, g: null };
    expect(["a", "b", "c", "d", "e", "g", "missing"].map((key) => scalar(values, key))).toEqual([
      "text",
      3,
      false,
      null,
      null,
      null,
      null,
    ]);
    expect(scalar(undefined, "a")).toBeNull();
  });

  it("reads the text entries of a list and ignores the rest", () => {
    expect(names({ a: ["x", 1, "y", null] }, "a")).toEqual(["x", "y"]);
    expect(names({ a: "x" }, "a")).toEqual([]);
    expect(names(undefined, "a")).toEqual([]);
  });

  it("reads a map of scalars as text and ignores nested values", () => {
    expect(
      textMap({ tags: { team: "orders", count: 2, on: true, nested: { a: 1 }, none: null } }, "tags"),
    ).toEqual({
      team: "orders",
      count: "2",
      on: "true",
    });
    expect(textMap({ tags: ["a"] }, "tags")).toEqual({});
    expect(textMap({ tags: null }, "tags")).toEqual({});
  });

  it("reads the objects of a list and ignores the rest", () => {
    expect(objects({ members: [{ id: 1 }, "x", null, [1], { id: 2 }] }, "members")).toEqual([
      { id: 1 },
      { id: 2 },
    ]);
    expect(objects({}, "members")).toEqual([]);
  });

  it("shows nothing as a dash, text as it is and other values as JSON", () => {
    expect([null, undefined, "", "text", 0, false, { a: 1 }, [1, 2]].map(displayValue)).toEqual([
      "—",
      "—",
      "—",
      "text",
      "0",
      "false",
      '{"a":1}',
      "[1,2]",
    ]);
  });
});
