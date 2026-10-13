import { useId } from "react";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

/**
 * A number; clearing the input removes the value. The schema's `minimum` and `maximum` and the hint's `step`
 * bound the input.
 *
 * @example
 * <NumberField name="min_capacity" schema={{ type: "number", minimum: 0.5 }} ui={{ step: 0.5 }} value={1} onChange={setValue} context={{}} formValue={{}} />
 */
export function NumberField({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const inputId = useId();
  const minimum = typeof schema.minimum === "number" ? schema.minimum : undefined;
  const maximum = typeof schema.maximum === "number" ? schema.maximum : undefined;
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        {fieldLabel(name, schema, ui)}
      </label>
      <Input
        id={inputId}
        type="number"
        min={minimum}
        max={maximum}
        step={ui.step}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
        className="max-w-40 font-mono"
      />
    </div>
  );
}
