import { describe, expect, it } from "vitest";
import { resourceIdFromArn } from "./resourceId";

describe("resourceIdFromArn", () => {
  it("matches the id the backend gives the same ARN", () => {
    expect(resourceIdFromArn("arn:aws:rds:eu-west-2:000000000000:db:demo-orders")).toBe(
      "YXJuOmF3czpyZHM6ZXUtd2VzdC0yOjAwMDAwMDAwMDAwMDpkYjpkZW1vLW9yZGVycw",
    );
  });

  it("uses the URL-safe alphabet, so the id can sit in a path", () => {
    // ">>>???" is "Pj4+Pz8/" in the standard alphabet.
    expect(resourceIdFromArn(">>>???")).toBe("Pj4-Pz8_");
  });

  it("drops the padding", () => {
    expect(resourceIdFromArn("a")).toBe("YQ");
    expect(resourceIdFromArn("ab")).toBe("YWI");
  });
});
