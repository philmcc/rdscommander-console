import type { EngineVersionList, ResourcePage } from "@/api/types";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

/** One choice of the picker. */
interface VersionOption {
  value: string;
  label: string;
}

/**
 * Order two engine versions newest first, comparing each dotted part as a number.
 *
 * @example
 * ["16.4", "16.10", "15.8"].sort(newestVersionFirst); // ["16.10", "16.4", "15.8"]
 */
export function newestVersionFirst(first: string, second: string): number {
  const firstParts = first.split(".").map(Number);
  const secondParts = second.split(".").map(Number);
  for (let index = 0; index < Math.max(firstParts.length, secondParts.length); index += 1) {
    const difference = (secondParts[index] ?? 0) - (firstParts[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

/**
 * An engine version. On a task about an instance or cluster, the versions its current version can be upgraded
 * to, newest first, each marked major or minor; `ui.upgrade_kind` keeps only the minor targets (a minor
 * upgrade) or only the major ones (a major upgrade). On a task with no resource (a create), every version of
 * the engine `ui.engine` names, newest first, each with the parameter group family it uses.
 *
 * @example
 * <EngineVersionPicker name="engine_version" schema={{ type: "string" }} ui={{ widget: "engine_version", engine: "postgres" }} value="" onChange={setVersion} context={{ environmentSlug: "sandbox" }} formValue={{}} />
 */
export function EngineVersionPicker({
  name,
  schema,
  ui,
  value,
  onChange,
  problem,
  context,
  describedBy,
}: WidgetProps) {
  const forCreate = !context.resourceId && typeof ui.engine === "string";
  const resourcePage = useApiGet<ResourcePage>(
    context.resourceId ? `/api/v1/resources/${context.resourceId}` : null,
  );
  const facts = resourcePage.data?.record.facts ?? {};
  const engine = forCreate ? ui.engine : String(facts.engine ?? "postgres");
  const versions = useApiGet<EngineVersionList>(
    (forCreate || resourcePage.data) && context.environmentSlug ? "/api/v1/parameters/engine-versions" : null,
    { environment_slug: context.environmentSlug, engine },
  );
  const currentVersion = String(facts.engine_version ?? "");
  const options: VersionOption[] = forCreate
    ? [...(versions.data?.versions ?? [])]
        .sort((first, second) => newestVersionFirst(first.engine_version, second.engine_version))
        .map((version) => ({
          value: version.engine_version,
          label: `${version.engine_version} (${version.parameter_group_family})`,
        }))
    : (versions.data?.versions.find((v) => v.engine_version === currentVersion)?.valid_upgrade_targets ?? [])
        .filter(
          (target) => ui.upgrade_kind === undefined || target.is_major === (ui.upgrade_kind === "major"),
        )
        .sort((first, second) => newestVersionFirst(first.engine_version, second.engine_version))
        .map((target) => ({
          value: target.engine_version,
          label: `${target.engine_version}${target.is_major ? " (major)" : " (minor)"}`,
        }));
  const label = fieldLabel(name, schema, ui);
  const chosen = typeof value === "string" ? value : "";
  const error = resourcePage.error ?? versions.error;
  return (
    <label className="block space-y-1 text-sm">
      <span>{label}</span>
      <select
        aria-label={label}
        aria-invalid={problem ? true : undefined}
        aria-describedby={describedBy}
        value={chosen}
        onChange={(e) => onChange(e.target.value || undefined)}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2 font-mono"
      >
        <option value="">Choose…</option>
        {chosen && !options.some((option) => option.value === chosen) && (
          <option value={chosen}>{chosen}</option>
        )}
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {error && <span className="block text-xs text-status-red">Could not list versions: {error}</span>}
    </label>
  );
}
