import { useId } from "react";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

/** Single-line text; identifiers are monospace. */
export function TextField({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const inputId = useId();
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        {fieldLabel(name, schema, ui)}
      </label>
      <Input
        id={inputId}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        className="font-mono"
      />
    </div>
  );
}
