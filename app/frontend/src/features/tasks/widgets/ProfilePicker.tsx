import type { ProfileList } from "@/api/types";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

/** An AWS CLI profile from this machine, or none. */
export function ProfilePicker({ name, schema, ui, value, onChange, problem, describedBy }: WidgetProps) {
  const profiles = useApiGet<ProfileList>("/api/v1/environments/import/profiles");
  const label = fieldLabel(name, schema, ui);
  return (
    <label className="block space-y-1 text-sm">
      <span>{label}</span>
      <select
        aria-label={label}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={typeof value === "string" ? value : ""}
        onChange={(e) => onChange(e.target.value || null)}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2 font-mono"
      >
        <option value="">None</option>
        {profiles.data?.profiles.map((profile) => (
          <option key={profile} value={profile}>
            {profile}
          </option>
        ))}
      </select>
      {profiles.error && (
        <span className="block text-xs text-status-red">Could not list profiles: {profiles.error}</span>
      )}
    </label>
  );
}
