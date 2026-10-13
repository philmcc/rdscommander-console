import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type ObjectSchema, SchemaForm } from "../SchemaForm";
import { DateTimeField, inputTextOfUtc, utcOfInputText } from "./DateTimeField";

afterEach(() => vi.unstubAllGlobals());

const WINDOW = {
  earliest_utc: "2026-10-04T09:55:00Z",
  latest_restorable_utc: "2026-10-11T12:00:00Z",
  unknown_reason: null,
};

/** The point-in-time restore form's two related fields. */
const FORM_SCHEMA: ObjectSchema = {
  type: "object",
  properties: {
    restore_time_utc: { type: "string", ui: { widget: "datetime", label: "Restore to" } },
    use_latest_restorable_time: { type: "boolean", default: true, ui: { widget: "boolean" } },
  },
};

function stubWindow(window: object = WINDOW) {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/lifecycle/restore-window/abc") return Response.json(window);
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

/** The field inside a form that keeps the whole value, as the task panel does. */
function renderInForm(initial: Record<string, unknown> = {}) {
  const seen: Record<string, unknown>[] = [];
  function Harness() {
    const [form, setForm] = useState<Record<string, unknown>>(initial);
    const update = (next: Record<string, unknown>) => {
      seen.push(next);
      setForm(next);
    };
    return (
      <DateTimeField
        name="restore_time_utc"
        schema={{ type: "string" }}
        ui={{ widget: "datetime", label: "Restore to" }}
        value={form.restore_time_utc}
        onChange={(next) => update({ ...form, restore_time_utc: next })}
        onFormChange={(patch) => update({ ...form, ...patch })}
        context={{ environmentSlug: "sandbox", resourceId: "abc", resourceKind: "instance" }}
        formValue={form}
        formSchema={FORM_SCHEMA}
      />
    );
  }
  render(<Harness />);
  return seen;
}

describe("utcOfInputText and inputTextOfUtc", () => {
  it("reads the input's text as UTC with seconds", () => {
    expect(utcOfInputText("2026-10-11T10:00")).toBe("2026-10-11T10:00:00Z");
    expect(utcOfInputText("2026-10-11T10:00:30")).toBe("2026-10-11T10:00:30Z");
    expect(utcOfInputText("")).toBeNull();
    expect(utcOfInputText("tomorrow")).toBeNull();
  });

  it("shows a UTC time, in any zone, as the input's text", () => {
    expect(inputTextOfUtc("2026-10-11T10:00:00Z")).toBe("2026-10-11T10:00:00");
    expect(inputTextOfUtc("2026-10-11T12:00:00+02:00")).toBe("2026-10-11T10:00:00");
    expect(inputTextOfUtc(undefined)).toBe("");
    expect(inputTextOfUtc("not a time")).toBe("");
  });
});

describe("DateTimeField", () => {
  it("shows the restore window and marks a time before it", async () => {
    stubWindow();
    renderInForm({ restore_time_utc: "2026-10-01T10:00:00Z", use_latest_restorable_time: false });
    expect(await screen.findByText(/2026-10-04 09:55:00 to 2026-10-11 12:00:00 UTC/)).toBeInTheDocument();
    expect(screen.getByText(/outside the restore window/)).toBeInTheDocument();
    expect(screen.getByLabelText("Restore to (UTC)")).toHaveAttribute("aria-invalid", "true");
  });

  it("emits a chosen time as ISO UTC with seconds and turns the latest restorable time off", async () => {
    stubWindow();
    const seen = renderInForm();
    await screen.findByText(/2026-10-04 09:55:00 to/);
    fireEvent.change(screen.getByLabelText("Restore to (UTC)"), { target: { value: "2026-10-11T10:00" } });
    expect(seen.at(-1)).toEqual({
      restore_time_utc: "2026-10-11T10:00:00Z",
      use_latest_restorable_time: false,
    });
    expect(screen.queryByText(/outside the restore window/)).toBeNull();
  });

  it("marks a time after the latest restorable time", async () => {
    stubWindow();
    renderInForm();
    await screen.findByText(/2026-10-04 09:55:00 to/);
    fireEvent.change(screen.getByLabelText("Restore to (UTC)"), { target: { value: "2026-10-11T13:00" } });
    expect(screen.getByText(/outside the restore window/)).toBeInTheDocument();
  });

  it("goes back to the latest restorable time, sending no time", async () => {
    stubWindow();
    const seen = renderInForm({
      restore_time_utc: "2026-10-08T10:00:00Z",
      use_latest_restorable_time: false,
    });
    await userEvent.click(screen.getByRole("button", { name: "Use the latest restorable time" }));
    const last = seen.at(-1) ?? {};
    expect(last.use_latest_restorable_time).toBe(true);
    expect(JSON.parse(JSON.stringify(last))).toEqual({ use_latest_restorable_time: true });
    expect(screen.getByLabelText("Restore to (UTC)")).toHaveValue("");
  });

  it("says why the window is unknown", async () => {
    stubWindow({
      earliest_utc: null,
      latest_restorable_utc: null,
      unknown_reason: "automated backups are off",
    });
    renderInForm();
    expect(await screen.findByText(/automated backups are off/)).toBeInTheDocument();
  });

  it("emits through onChange when the form has no latest-time field", async () => {
    stubWindow();
    const onChange = vi.fn();
    render(
      <DateTimeField
        name="when"
        schema={{ type: "string" }}
        ui={{ widget: "datetime", label: "When" }}
        value={undefined}
        onChange={onChange}
        context={{}}
        formValue={{}}
      />,
    );
    fireEvent.change(screen.getByLabelText("When (UTC)"), { target: { value: "2026-10-11T10:00" } });
    expect(onChange).toHaveBeenLastCalledWith("2026-10-11T10:00:00Z");
    await waitFor(() => expect(screen.queryByText(/Restore window/)).toBeNull());
  });
});

describe("DateTimeField with the latest-restorable-time switch", () => {
  /** The point-in-time restore's two fields in a whole form, as the task panel renders them. */
  function renderPointInTimeForm(initial: Record<string, unknown>) {
    const posted: Record<string, unknown>[] = [];
    function Harness() {
      const [form, setForm] = useState<Record<string, unknown>>(initial);
      return (
        <SchemaForm
          schema={{
            type: "object",
            properties: {
              restore_time_utc: { type: "string", ui: { widget: "datetime", label: "Restore to", order: 1 } },
              use_latest_restorable_time: {
                type: "boolean",
                default: true,
                ui: { widget: "boolean", label: "Latest restorable time", order: 2 },
              },
            },
          }}
          value={form}
          onChange={(next) => {
            posted.push(JSON.parse(JSON.stringify(next)) as Record<string, unknown>);
            setForm(next);
          }}
          problems={{}}
          context={{ environmentSlug: "sandbox", resourceId: "abc", resourceKind: "instance" }}
        />
      );
    }
    render(<Harness />);
    return posted;
  }

  it("clears the chosen time when the latest restorable time is switched on", async () => {
    stubWindow();
    const posted = renderPointInTimeForm({
      restore_time_utc: "2026-10-08T10:00:00Z",
      use_latest_restorable_time: false,
    });
    await userEvent.click(screen.getByRole("switch", { name: "Latest restorable time" }));
    await waitFor(() => expect(posted.at(-1)).toEqual({ use_latest_restorable_time: true }));
    expect(screen.getByLabelText("Restore to (UTC)")).toHaveValue("");
    expect(screen.queryByText(/only one can be used/)).toBeNull();
  });

  it("says a time is needed when the latest restorable time is switched off without one", async () => {
    stubWindow();
    const posted = renderPointInTimeForm({});
    await userEvent.click(screen.getByRole("switch", { name: "Latest restorable time" }));
    expect(posted.at(-1)).toEqual({ use_latest_restorable_time: false });
    const timeInput = screen.getByLabelText("Restore to (UTC)");
    expect(timeInput).toHaveAttribute("aria-invalid", "true");
    expect(timeInput).toHaveAccessibleDescription(
      /choose a restore time, or use the latest restorable time/i,
    );
    fireEvent.change(timeInput, { target: { value: "2026-10-08T10:00" } });
    expect(posted.at(-1)).toEqual({
      restore_time_utc: "2026-10-08T10:00:00Z",
      use_latest_restorable_time: false,
    });
    expect(screen.queryByText(/choose a restore time, or use the latest restorable time/i)).toBeNull();
  });
});
