import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { WidgetProps } from "../SchemaForm";
import { formatWindow, parseWindow, WindowField } from "./WindowField";

describe("parseWindow and formatWindow", () => {
  it("splits a weekly window into days and times", () => {
    expect(parseWindow("sun:03:00-mon:04:30", "weekly")).toEqual({
      startDay: "sun",
      startTime: "03:00",
      endDay: "mon",
      endTime: "04:30",
    });
  });

  it("splits a daily window into times only", () => {
    expect(parseWindow("03:00-04:00", "daily")).toEqual({ startTime: "03:00", endTime: "04:00" });
  });

  it("returns null for text that is not a window of that kind", () => {
    expect(parseWindow("03:00-04:00", "weekly")).toBeNull();
    expect(parseWindow("sun:03:00-sun:04:00", "daily")).toBeNull();
    expect(parseWindow(undefined, "daily")).toBeNull();
  });

  it("writes the backend string back for both kinds", () => {
    expect(
      formatWindow({ startDay: "sat", startTime: "01:00", endDay: "sat", endTime: "02:00" }, "weekly"),
    ).toBe("sat:01:00-sat:02:00");
    expect(formatWindow({ startTime: "01:00", endTime: "02:00" }, "daily")).toBe("01:00-02:00");
  });
});

function renderField(overrides: Partial<WidgetProps>) {
  const onChange = vi.fn();
  render(
    <WindowField
      name="preferred_maintenance_window"
      schema={{ type: "string" }}
      ui={{ widget: "window", window_kind: "weekly", label: "Maintenance window" }}
      value="sun:03:00-sun:04:00"
      onChange={onChange}
      context={{ environmentSlug: "sandbox", resourceId: "abc" }}
      formValue={{}}
      {...overrides}
    />,
  );
  return onChange;
}

describe("WindowField", () => {
  it("shows the weekly window as two days and two times", () => {
    renderField({});
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("sun");
    expect(screen.getByLabelText("Maintenance window start time")).toHaveValue("03:00");
    expect(screen.getByLabelText("Maintenance window end day")).toHaveValue("sun");
    expect(screen.getByLabelText("Maintenance window end time")).toHaveValue("04:00");
  });

  it("emits the backend string when the end time changes", () => {
    const onChange = renderField({});
    fireEvent.change(screen.getByLabelText("Maintenance window end time"), { target: { value: "04:30" } });
    expect(onChange).toHaveBeenLastCalledWith("sun:03:00-sun:04:30");
  });

  it("emits the backend string when the end day changes", async () => {
    const onChange = renderField({});
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window end day"), "mon");
    expect(onChange).toHaveBeenLastCalledWith("sun:03:00-mon:04:00");
  });

  it("shows only times for a daily window", () => {
    const onChange = renderField({
      ui: { widget: "window", window_kind: "daily", label: "Backup window" },
      value: "03:00-04:00",
    });
    expect(screen.queryByLabelText(/day/)).toBeNull();
    fireEvent.change(screen.getByLabelText("Backup window start time"), { target: { value: "02:00" } });
    expect(onChange).toHaveBeenLastCalledWith("02:00-04:00");
  });

  it("says a window shorter than 30 minutes is too short and still emits it", async () => {
    const onChange = renderField({ value: "sun:03:00-sun:03:15" });
    expect(screen.getByText(/at least 30 minutes/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window start day"), "mon");
    expect(onChange).toHaveBeenLastCalledWith("mon:03:00-sun:03:15");
  });

  it("does not warn about a window of 30 minutes or more, including across midnight", () => {
    renderField({ ui: { widget: "window", window_kind: "daily" }, value: "23:30-00:30" });
    expect(screen.queryByText(/at least 30 minutes/)).toBeNull();
  });

  it("marks the controls invalid when the server reports a problem", () => {
    renderField({ problem: "overlaps the backup window", describedBy: "x-problem" });
    expect(screen.getByLabelText("Maintenance window start time")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Maintenance window start time")).toHaveAttribute(
      "aria-describedby",
      "x-problem",
    );
  });

  it("shows an unset weekly window empty and emits nothing until all four parts are chosen", async () => {
    const onChange = renderField({ value: undefined });
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window end day")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window start time")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window end time")).toHaveValue("");
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window start day"), "sat");
    fireEvent.change(screen.getByLabelText("Maintenance window start time"), { target: { value: "01:00" } });
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window end day"), "sat");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Maintenance window end time"), { target: { value: "02:00" } });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith("sat:01:00-sat:02:00");
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("sat");
  });

  it("emits an unset daily window once both times are chosen", () => {
    const onChange = renderField({
      ui: { widget: "window", window_kind: "daily", label: "Backup window" },
      value: undefined,
    });
    fireEvent.change(screen.getByLabelText("Backup window start time"), { target: { value: "01:00" } });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Backup window end time"), { target: { value: "02:00" } });
    expect(onChange).toHaveBeenLastCalledWith("01:00-02:00");
  });

  it("names a value that is not a window of this kind and leaves the controls empty", () => {
    renderField({ value: "03:00-04:00" });
    expect(
      screen.getByText("Existing value 03:00-04:00 is not a valid window; choose a new one"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window start time")).toHaveValue("");
  });

  it("does not show the invalid-value note for a valid or unset value", () => {
    renderField({ value: undefined });
    expect(screen.queryByText(/is not a valid window/)).toBeNull();
  });

  it("empties the controls again when the form resets the value after a window was chosen", async () => {
    const onChange = vi.fn();
    const props = {
      name: "preferred_maintenance_window",
      schema: { type: "string" },
      ui: { widget: "window", window_kind: "weekly" as const, label: "Maintenance window" },
      onChange,
      context: {},
      formValue: {},
    };
    const { rerender } = render(<WindowField {...props} value={undefined} />);
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window start day"), "sat");
    fireEvent.change(screen.getByLabelText("Maintenance window start time"), { target: { value: "01:00" } });
    await userEvent.selectOptions(screen.getByLabelText("Maintenance window end day"), "sat");
    fireEvent.change(screen.getByLabelText("Maintenance window end time"), { target: { value: "02:00" } });
    expect(onChange).toHaveBeenCalledTimes(1);
    rerender(<WindowField {...props} value="sat:01:00-sat:02:00" />);
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("sat");
    rerender(<WindowField {...props} value={undefined} />);
    expect(screen.getByLabelText("Maintenance window start day")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window start time")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window end day")).toHaveValue("");
    expect(screen.getByLabelText("Maintenance window end time")).toHaveValue("");
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
