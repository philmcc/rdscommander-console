import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OwnershipBadge } from "./OwnershipBadge";

describe("OwnershipBadge", () => {
  it.each([
    ["adopted", "adopted", "text-status-green"],
    ["foreign_terraform", "Terraform-owned", "text-status-amber"],
    ["foreign_cloudformation", "CloudFormation-owned", "text-status-amber"],
    ["unmanaged", "unmanaged", "text-muted"],
  ])("renders %s as %s in the %s token colour", (ownership, label, colourClass) => {
    render(<OwnershipBadge ownership={ownership} />);
    expect(screen.getByText(label).className).toContain(colourClass);
  });
});
