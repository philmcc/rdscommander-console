import { describe, expect, it } from "vitest";
import { ApiError } from "@/api/client";
import { describeError } from "./describeError";

describe("describeError", () => {
  it("gives the detail the backend sent", () => {
    expect(describeError(new ApiError(404, "no such environment"))).toBe("no such environment");
  });

  it("falls back to the status when the response had no text", () => {
    expect(describeError(new ApiError(500, ""))).toBe("the server answered 500");
  });

  it("gives the message of any other error, and the text of anything else", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toBe("Failed to fetch");
    expect(describeError(new Error(""))).toBe("the request failed");
    expect(describeError("odd")).toBe("odd");
  });
});
