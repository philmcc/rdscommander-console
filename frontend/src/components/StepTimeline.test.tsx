import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StepAttemptSummary } from "@/api/types";
import { StepTimeline } from "./StepTimeline";

function attempt(overrides: Partial<StepAttemptSummary>): StepAttemptSummary {
  return {
    attempt_number: 1,
    detail: "",
    finished_at_utc: null,
    id: 1,
    observation_state: "ABSENT",
    outcome_status: null,
    output: {},
    started_at_utc: "2026-10-10T10:00:00Z",
    step_name: "create_snapshot",
    ...overrides,
  };
}

describe("StepTimeline", () => {
  it("shows one row per step in order with the latest attempt's outcome", () => {
    render(
      <StepTimeline
        stepNames={["create_snapshot", "apply_parameters", "reboot_instance"]}
        attempts={[
          attempt({ id: 3, attempt_number: 2, outcome_status: "completed", detail: "snapshot ready" }),
          attempt({ id: 2, attempt_number: 1, outcome_status: "failed", detail: "throttled" }),
          attempt({ id: 4, step_name: "apply_parameters", observation_state: "PENDING" }),
        ]}
      />,
    );
    const rows = screen.getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "completedcreate_snapshotsnapshot ready",
      "runningapply_parameters",
      "pendingreboot_instance",
    ]);
    expect(screen.getByText("completed").className).toContain("text-status-green");
    expect(screen.getByText("running").className).toContain("text-status-blue");
    expect(screen.getByText("pending").className).toContain("text-status-amber");
    expect(screen.queryByText("throttled")).not.toBeInTheDocument();
  });

  it("falls back to the attempted step names when none are supplied", () => {
    render(
      <StepTimeline stepNames={[]} attempts={[attempt({ outcome_status: "failed", detail: "boom" })]} />,
    );
    expect(screen.getByText("create_snapshot")).toBeInTheDocument();
    expect(screen.getByText("failed").className).toContain("text-status-red");
  });
});
