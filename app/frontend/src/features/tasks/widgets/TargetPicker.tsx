import { useEnvironment } from "@/app/EnvironmentContext";
import type { WidgetProps } from "../SchemaForm";
import { CheckboxList } from "./CheckboxList";
import { fieldLabel } from "./fieldLabel";

/**
 * A target (account and region) of the environment: a select for a single `target_key`, checkboxes when the
 * property is a list of keys.
 */
export function TargetPicker({
  name,
  schema,
  ui,
  value,
  onChange,
  problem,
  context,
  describedBy,
}: WidgetProps) {
  const { environments } = useEnvironment();
  const targets = environments.find((e) => e.slug === context.environmentSlug)?.targets ?? [];
  const options = targets.map((t) => ({
    value: t.target_key,
    label: `${t.aws_account_id} / ${t.aws_region}${t.simulated ? " (simulated)" : ""}`,
  }));
  const label = fieldLabel(name, schema, ui);
  if (schema.type === "array") {
    const selected = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    return (
      <CheckboxList
        legend={label}
        options={options}
        selected={selected}
        onChange={onChange}
        describedBy={describedBy}
        emptyMessage="This environment has no targets."
      />
    );
  }
  return (
    <label className="block space-y-1 text-sm">
      <span>{label}</span>
      <select
        aria-label={label}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value || undefined)}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2 font-mono"
      >
        <option value="">Choose…</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
