import { useId, useMemo } from "react";
import type { EngineVersionList } from "@/api/types";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

/** A parameter group family: free text, with the families of the environment's engine versions suggested. */
export function FamilyPicker({
  name,
  schema,
  ui,
  value,
  onChange,
  problem,
  context,
  formValue,
  describedBy,
}: WidgetProps) {
  const inputId = useId();
  const listId = useId();
  const engine = formValue.scope === "cluster" ? "aurora-postgresql" : "postgres";
  const versions = useApiGet<EngineVersionList>(
    context.environmentSlug ? "/api/v1/parameters/engine-versions" : null,
    { environment_slug: context.environmentSlug, engine },
  );
  const families = useMemo(
    () => [...new Set((versions.data?.versions ?? []).map((v) => v.parameter_group_family))].sort(),
    [versions.data],
  );
  const label = fieldLabel(name, schema, ui);
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={inputId} className="block">
        {label}
      </label>
      <Input
        id={inputId}
        list={listId}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        className="font-mono"
      />
      <datalist id={listId}>
        {families.map((family) => (
          <option key={family} value={family} />
        ))}
      </datalist>
    </div>
  );
}
