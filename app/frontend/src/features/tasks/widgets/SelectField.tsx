import { useId } from "react";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

const MAX_RADIO_CHOICES = 4;

/**
 * One of a few fixed choices: a radio group for up to four, a select for more. A choice is sent as the
 * schema's own value, so an integer enum (enhanced monitoring's seconds) sends a number, not its text.
 */
export function SelectField({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const labelId = useId();
  const choices = schema.enum ?? schema.anyOf?.flatMap((s) => s.enum ?? []) ?? [];
  const title = fieldLabel(name, schema, ui);
  const label = (choice: string | number) => ui.labels?.[String(choice)] ?? String(choice);
  if (choices.length <= MAX_RADIO_CHOICES) {
    return (
      <div
        role="radiogroup"
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        className="space-y-1 text-sm"
      >
        <span id={labelId} className="block">
          {title}
        </span>
        <div className="flex flex-wrap gap-2">
          {choices.map((choice) => (
            <label
              key={choice}
              className="flex items-center gap-1 rounded-md border border-hairline px-2 py-1"
            >
              <input
                type="radio"
                name={name}
                value={choice}
                checked={value === choice}
                onChange={() => onChange(choice)}
                aria-label={label(choice)}
              />
              {label(choice)}
            </label>
          ))}
        </div>
      </div>
    );
  }
  return (
    <label className="block space-y-1 text-sm">
      <span>{title}</span>
      <select
        aria-label={title}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(choices.find((choice) => String(choice) === e.target.value))}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2"
      >
        <option value="">Choose…</option>
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {label(choice)}
          </option>
        ))}
      </select>
    </label>
  );
}
