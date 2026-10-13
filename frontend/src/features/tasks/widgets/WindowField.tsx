import { useId, useState } from "react";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

/** Which shape of window a field holds: `hh:mm-hh:mm` each day, or `ddd:hh:mm-ddd:hh:mm` within the week. */
export type WindowKind = "daily" | "weekly";

/** The parts of a window; the days exist only for a weekly one. Times are `hh:mm`, days are `mon`..`sun`. */
export interface WindowParts {
  startDay?: string;
  startTime: string;
  endDay?: string;
  endTime: string;
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
const MINUTES_PER_DAY = 24 * 60;
/** AWS refuses a maintenance or backup window shorter than this. */
const MINIMUM_WINDOW_MINUTES = 30;
const TIME = "([01]\\d|2[0-3]):([0-5]\\d)";
const DAY = `(${DAYS.join("|")})`;
const WEEKLY_PATTERN = new RegExp(`^${DAY}:${TIME}-${DAY}:${TIME}$`);
const DAILY_PATTERN = new RegExp(`^${TIME}-${TIME}$`);

/**
 * Split a window string into its parts.
 *
 * @param value The backend's window text; anything else (unset, malformed) gives `null`.
 * @param kind `weekly` expects `ddd:hh:mm-ddd:hh:mm`, `daily` expects `hh:mm-hh:mm`.
 * @returns The parts, or `null` when `value` is not a window of that kind.
 *
 * @example
 * parseWindow("sun:03:00-mon:04:30", "weekly"); // { startDay: "sun", startTime: "03:00", endDay: "mon", endTime: "04:30" }
 * parseWindow("03:00-04:00", "daily"); // { startTime: "03:00", endTime: "04:00" }
 * parseWindow("03:00-04:00", "weekly"); // null
 */
export function parseWindow(value: unknown, kind: WindowKind): WindowParts | null {
  if (typeof value !== "string") return null;
  if (kind === "weekly") {
    const match = WEEKLY_PATTERN.exec(value);
    if (!match) return null;
    const [, startDay, startHour, startMinute, endDay, endHour, endMinute] = match;
    return {
      startDay,
      startTime: `${startHour}:${startMinute}`,
      endDay,
      endTime: `${endHour}:${endMinute}`,
    };
  }
  const match = DAILY_PATTERN.exec(value);
  if (!match) return null;
  const [, startHour, startMinute, endHour, endMinute] = match;
  return { startTime: `${startHour}:${startMinute}`, endTime: `${endHour}:${endMinute}` };
}

/**
 * Write window parts as the backend's window string; the inverse of `parseWindow`.
 *
 * @param parts The start and end; the days are used for `weekly` and ignored for `daily`.
 * @param kind Which shape to write.
 * @returns `ddd:hh:mm-ddd:hh:mm` or `hh:mm-hh:mm`.
 *
 * @example
 * formatWindow({ startDay: "sat", startTime: "01:00", endDay: "sat", endTime: "02:00" }, "weekly"); // "sat:01:00-sat:02:00"
 * formatWindow({ startTime: "01:00", endTime: "02:00" }, "daily"); // "01:00-02:00"
 */
export function formatWindow(parts: WindowParts, kind: WindowKind): string {
  if (kind === "weekly")
    return `${parts.startDay ?? "mon"}:${parts.startTime}-${parts.endDay ?? "mon"}:${parts.endTime}`;
  return `${parts.startTime}-${parts.endTime}`;
}

function minutesIntoPeriod(day: string | undefined, time: string): number {
  const [hours = "0", minutes = "0"] = time.split(":");
  const dayIndex = day === undefined ? 0 : Math.max(DAYS.indexOf(day as (typeof DAYS)[number]), 0);
  return dayIndex * MINUTES_PER_DAY + Number(hours) * 60 + Number(minutes);
}

/** Minutes from start to end, wrapping past midnight (daily) or past the end of the week (weekly). */
function lengthInMinutes(parts: WindowParts, kind: WindowKind): number {
  const period = kind === "weekly" ? 7 * MINUTES_PER_DAY : MINUTES_PER_DAY;
  const start = minutesIntoPeriod(parts.startDay, parts.startTime);
  const end = minutesIntoPeriod(parts.endDay, parts.endTime);
  return (end - start + period) % period;
}

/** The parts a person has chosen so far for a window that has no valid value yet; `""` is not chosen. */
interface DraftParts {
  startDay: string;
  startTime: string;
  endDay: string;
  endTime: string;
}

const EMPTY_DRAFT: DraftParts = { startDay: "", startTime: "", endDay: "", endTime: "" };

/** The draft as window parts when every part the kind needs has been chosen, else `null`. */
function completeParts(draft: DraftParts, kind: WindowKind): WindowParts | null {
  if (draft.startTime === "" || draft.endTime === "") return null;
  if (kind === "daily") return { startTime: draft.startTime, endTime: draft.endTime };
  if (draft.startDay === "" || draft.endDay === "") return null;
  return { ...draft };
}

const CONTROL_CLASS = "h-8 rounded-md border border-hairline bg-surface px-2 font-mono";

/**
 * A maintenance or backup window as day and time controls (UTC). `ui.window_kind` picks weekly (two days and
 * two times) or daily (two times). With a valid value the controls show it and every change is emitted as the
 * backend's string, even when the window is shorter than the 30 minutes AWS wants (a note says so; the backend
 * has the final say). With no value the controls are empty and nothing is emitted until every part has been
 * chosen. A value that is not a window of this kind is quoted in a note, the controls stay empty, and a new
 * window must be chosen.
 *
 * @example
 * <WindowField name="preferred_maintenance_window" schema={{ type: "string" }} ui={{ widget: "window", window_kind: "weekly" }} value="sun:03:00-sun:04:00" onChange={setWindow} context={{}} formValue={{}} />
 */
export function WindowField({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const shortNoticeId = useId();
  const invalidNoticeId = useId();
  const kind: WindowKind = ui.window_kind === "daily" ? "daily" : "weekly";
  const label = fieldLabel(name, schema, ui);
  const parsed = parseWindow(value, kind);
  const unreadableText = parsed === null && typeof value === "string" && value !== "" ? value : null;
  // The choices made so far for a window without a valid value. A value that arrives different from the one
  // before it, and is not the window this draft just emitted, came from outside (a form reset, a restored
  // draft) and drops the draft; the draft's own emit coming back as the value keeps it.
  const [draftState, setDraftState] = useState<{ parts: DraftParts; emittedText: string | null }>({
    parts: EMPTY_DRAFT,
    emittedText: null,
  });
  const [previousValue, setPreviousValue] = useState<unknown>(value);
  let draft = draftState.parts;
  if (value !== previousValue) {
    setPreviousValue(value);
    if (value !== draftState.emittedText) {
      setDraftState({ parts: EMPTY_DRAFT, emittedText: null });
      draft = EMPTY_DRAFT;
    }
  }
  const shown: DraftParts = parsed
    ? {
        startDay: parsed.startDay ?? "",
        startTime: parsed.startTime,
        endDay: parsed.endDay ?? "",
        endTime: parsed.endTime,
      }
    : draft;
  const lengthParts = parsed ?? completeParts(draft, kind);
  const isTooShort = lengthParts !== null && lengthInMinutes(lengthParts, kind) < MINIMUM_WINDOW_MINUTES;
  const change = (patch: Partial<DraftParts>) => {
    const next = { ...shown, ...patch };
    const complete = completeParts(next, kind);
    const emittedText = complete ? formatWindow(complete, kind) : null;
    if (parsed === null) setDraftState({ parts: next, emittedText });
    if (emittedText !== null) onChange(emittedText);
  };
  const describedByIds =
    [
      describedBy,
      unreadableText !== null ? invalidNoticeId : undefined,
      isTooShort ? shortNoticeId : undefined,
    ]
      .filter(Boolean)
      .join(" ") || undefined;
  const controlProps = {
    "aria-invalid": problem || unreadableText !== null ? (true as const) : undefined,
    "aria-describedby": describedByIds,
  };
  const dayControl = (which: "start" | "end") => (
    <select
      aria-label={`${label} ${which} day`}
      {...controlProps}
      value={which === "start" ? shown.startDay : shown.endDay}
      onChange={(e) => change(which === "start" ? { startDay: e.target.value } : { endDay: e.target.value })}
      className={CONTROL_CLASS}
    >
      <option value="">Day</option>
      {DAYS.map((day) => (
        <option key={day} value={day}>
          {day}
        </option>
      ))}
    </select>
  );
  const timeControl = (which: "start" | "end") => (
    <input
      type="time"
      step={1800}
      aria-label={`${label} ${which} time`}
      {...controlProps}
      value={which === "start" ? shown.startTime : shown.endTime}
      onChange={(e) => {
        // Clearing a time of a valid window would make it incomplete; only a draft may hold a blank.
        if (e.target.value === "" && parsed !== null) return;
        change(which === "start" ? { startTime: e.target.value } : { endTime: e.target.value });
      }}
      className={CONTROL_CLASS}
    />
  );
  return (
    <div className="space-y-1 text-sm">
      <span className="block">{label} (UTC)</span>
      <div className="flex flex-wrap items-center gap-2">
        {kind === "weekly" && dayControl("start")}
        {timeControl("start")}
        <span aria-hidden="true">to</span>
        {kind === "weekly" && dayControl("end")}
        {timeControl("end")}
      </div>
      {unreadableText !== null && (
        <p id={invalidNoticeId} className="text-xs text-status-amber">
          Existing value {unreadableText} is not a valid window; choose a new one
        </p>
      )}
      {isTooShort && (
        <p id={shortNoticeId} className="text-xs text-status-amber">
          A window should be at least 30 minutes long.
        </p>
      )}
    </div>
  );
}
