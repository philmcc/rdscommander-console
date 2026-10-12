import { useId } from "react";
import { Switch } from "@/components/ui/switch";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";

/** An on/off switch. */
export function BooleanField({ name, schema, ui, value, onChange, describedBy }: WidgetProps) {
  const switchId = useId();
  return (
    <div className="flex items-center gap-3 text-sm">
      <Switch
        id={switchId}
        aria-describedby={describedBy}
        checked={value === true}
        onCheckedChange={(checked) => onChange(checked)}
      />
      <label htmlFor={switchId}>{fieldLabel(name, schema, ui)}</label>
    </div>
  );
}
