import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { WidgetProps } from "../SchemaForm";
import { SelectField } from "./SelectField";

const intervals: WidgetProps["schema"] = { type: "integer", enum: [0, 1, 5, 10, 15, 30, 60] };
const intervalHints: WidgetProps["ui"] = {
  label: "Enhanced monitoring",
  labels: { "0": "Off", "1": "1 s", "5": "5 s", "10": "10 s", "15": "15 s", "30": "30 s", "60": "60 s" },
};

function renderSelect(value: unknown) {
  const onChange = vi.fn();
  render(
    <SelectField
      name="monitoring_interval_seconds"
      schema={intervals}
      ui={intervalHints}
      value={value}
      onChange={onChange}
      context={{}}
      formValue={{}}
    />,
  );
  return onChange;
}

describe("SelectField with more than four choices", () => {
  it("sends a numeric choice as the number, not its text", async () => {
    // The walkthrough's "60 s" was sent as "60" and refused: "must be 0, 1, 5, 10, 15, 30 or 60".
    const onChange = renderSelect(undefined);
    await userEvent.selectOptions(screen.getByLabelText("Enhanced monitoring"), "60 s");
    expect(onChange).toHaveBeenCalledWith(60);
  });

  it("shows a numeric value as the chosen option", () => {
    renderSelect(30);
    expect(screen.getByLabelText("Enhanced monitoring")).toHaveValue("30");
  });

  it("clears the value when Choose… is picked again", async () => {
    const onChange = renderSelect(5);
    await userEvent.selectOptions(screen.getByLabelText("Enhanced monitoring"), "Choose…");
    expect(onChange).toHaveBeenCalledWith(undefined);
  });
});
