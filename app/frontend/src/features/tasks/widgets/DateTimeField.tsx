import { useEffect, useId } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

/** The times a point-in-time restore can choose from, as `GET /api/v1/lifecycle/restore-window/{id}` answers. */
interface RestoreWindow {
  earliest_utc: string | null;
  latest_restorable_utc: string | null;
  unknown_reason?: string | null;
}

/**
 * The option a point-in-time restore form sends instead of a time; the backend refuses both together, so a
 * chosen time turns it off and clearing the time turns it back on.
 */
const LATEST_RESTORABLE_TIME_FIELD = "use_latest_restorable_time";

/** Text a `datetime-local` input holds: `YYYY-MM-DDTHH:MM`, with `:SS` when seconds were given. */
const INPUT_TEXT_PATTERN = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(:\d{2})?(\.\d+)?$/;

/**
 * Read a `datetime-local` input's text as a UTC time, in the backend's form.
 *
 * @param text The input's value, such as `2026-10-11T10:00`; the field is labelled UTC, so it is read as UTC.
 * @returns ISO 8601 UTC with seconds and a `Z`, or `null` for empty or unreadable text.
 *
 * @example
 * utcOfInputText("2026-10-11T10:00"); // "2026-10-11T10:00:00Z"
 * utcOfInputText(""); // null
 */
export function utcOfInputText(text: string): string | null {
  const match = INPUT_TEXT_PATTERN.exec(text);
  if (!match) return null;
  const [, date, hoursAndMinutes, seconds] = match;
  const iso = `${date}T${hoursAndMinutes}${seconds ?? ":00"}Z`;
  return Number.isNaN(Date.parse(iso)) ? null : iso;
}

/**
 * Show a time, in any zone, as a `datetime-local` input's text in UTC; the inverse of `utcOfInputText`.
 *
 * @param value The option's value, such as `2026-10-11T10:00:00Z`; anything that is not a time gives `""`.
 * @returns `YYYY-MM-DDTHH:MM:SS` in UTC, or `""`.
 *
 * @example
 * inputTextOfUtc("2026-10-11T12:00:00+02:00"); // "2026-10-11T10:00:00"
 */
export function inputTextOfUtc(value: unknown): string {
  if (typeof value !== "string" || value === "") return "";
  const milliseconds = Date.parse(value);
  return Number.isNaN(milliseconds) ? "" : new Date(milliseconds).toISOString().slice(0, 19);
}

/** A UTC time as people read it: `2026-10-11 10:00:00`. */
function readable(iso: string): string {
  return inputTextOfUtc(iso).replace("T", " ");
}

/** Whether `iso` falls outside the window; a window that is not known marks nothing. */
function isOutsideWindow(iso: string | null, restoreWindow: RestoreWindow | null): boolean {
  if (iso === null || !restoreWindow?.earliest_utc || !restoreWindow.latest_restorable_utc) return false;
  const chosen = Date.parse(iso);
  return (
    chosen < Date.parse(restoreWindow.earliest_utc) ||
    chosen > Date.parse(restoreWindow.latest_restorable_utc)
  );
}

/**
 * A date and time in UTC, to the second, emitted as ISO 8601 UTC (`2026-10-11T10:00:00Z`). On a task about an
 * instance or cluster it shows the restore window read from `/lifecycle/restore-window/{resourceId}` and marks a
 * time outside it. When the form also has `use_latest_restorable_time` (the point-in-time restore), choosing a
 * time turns that off in the same update, and "Use the latest restorable time" (or switching that option on)
 * clears the time, so the form never sends both; with the option off and no time chosen, the input says a time
 * is needed.
 *
 * @example
 * <DateTimeField name="restore_time_utc" schema={{ type: "string" }} ui={{ widget: "datetime" }} value={undefined} onChange={setTime} context={{ resourceId: "abc" }} formValue={{}} />
 */
export function DateTimeField({
  name,
  schema,
  ui,
  value,
  onChange,
  onFormChange,
  problem,
  context,
  formValue,
  formSchema,
  describedBy,
}: WidgetProps) {
  const inputId = useId();
  const windowId = useId();
  const outsideId = useId();
  const missingTimeId = useId();
  // Only an instance or cluster has a restore window; a snapshot is restored as it was taken.
  const readsWindow = Boolean(context.resourceId) && context.resourceKind !== "snapshot";
  const restoreWindow = useApiGet<RestoreWindow>(
    readsWindow ? `/api/v1/lifecycle/restore-window/${context.resourceId}` : null,
  );
  const windowData = restoreWindow.data;
  const pairedWithLatest =
    onFormChange !== undefined && Object.hasOwn(formSchema?.properties ?? {}, LATEST_RESTORABLE_TIME_FIELD);
  const chosen = typeof value === "string" && value !== "" ? value : null;
  const outside = isOutsideWindow(chosen, windowData);
  const label = `${fieldLabel(name, schema, ui)} (UTC)`;

  const emit = (iso: string | null) => {
    if (pairedWithLatest && onFormChange) {
      onFormChange({ [name]: iso ?? undefined, [LATEST_RESTORABLE_TIME_FIELD]: iso === null });
    } else {
      onChange(iso ?? undefined);
    }
  };
  const known = windowData?.earliest_utc && windowData.latest_restorable_utc ? windowData : null;
  const latestSwitch = formValue[LATEST_RESTORABLE_TIME_FIELD];
  // The latest-time switch turned on while a time is chosen: the backend refuses both, so the time goes.
  const latestSwitchedOnOverTime = pairedWithLatest && chosen !== null && latestSwitch === true;
  useEffect(() => {
    if (latestSwitchedOnOverTime && onFormChange) onFormChange({ [name]: undefined });
  }, [latestSwitchedOnOverTime, onFormChange, name]);
  // The switch turned off and no time chosen: there is nothing to restore to yet.
  const missingTime = pairedWithLatest && chosen === null && latestSwitch === false;
  const describedByIds =
    [
      describedBy,
      readsWindow ? windowId : undefined,
      outside ? outsideId : undefined,
      missingTime ? missingTimeId : undefined,
    ]
      .filter(Boolean)
      .join(" ") || undefined;
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        {label}
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={inputId}
          type="datetime-local"
          step={1}
          min={known ? inputTextOfUtc(known.earliest_utc) : undefined}
          max={known ? inputTextOfUtc(known.latest_restorable_utc) : undefined}
          aria-invalid={problem || outside || missingTime ? true : undefined}
          aria-describedby={describedByIds}
          value={inputTextOfUtc(chosen)}
          onChange={(e) => emit(utcOfInputText(e.target.value))}
          className="w-auto max-w-64 font-mono"
        />
        {pairedWithLatest && chosen !== null && (
          <Button type="button" variant="ghost" size="sm" onClick={() => emit(null)}>
            Use the latest restorable time
          </Button>
        )}
      </div>
      {readsWindow && (
        <p id={windowId} className="text-xs text-muted">
          {known ? (
            <>
              Restore window:{" "}
              <span className="font-mono">
                {readable(known.earliest_utc ?? "")} to {readable(known.latest_restorable_utc ?? "")} UTC
              </span>
            </>
          ) : windowData?.unknown_reason ? (
            <>The restore window is not known: {windowData.unknown_reason}</>
          ) : restoreWindow.error ? (
            <>Could not read the restore window: {restoreWindow.error}</>
          ) : windowData ? (
            <>The restore window is not known.</>
          ) : (
            <>Reading the restore window…</>
          )}
        </p>
      )}
      {outside && (
        <p id={outsideId} className="text-xs text-status-amber">
          This time is outside the restore window; choose one between the times above.
        </p>
      )}
      {missingTime && (
        <p id={missingTimeId} className="text-xs text-status-red">
          Choose a restore time, or use the latest restorable time.
        </p>
      )}
    </div>
  );
}
