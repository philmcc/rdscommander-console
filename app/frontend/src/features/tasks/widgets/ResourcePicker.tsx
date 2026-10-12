import { useCallback, useId } from "react";
import type { EnvironmentInventory, ResourceList, ResourcePage, ResourceRecord } from "@/api/types";
import type { ObjectSchema, UiHints, WidgetProps } from "../SchemaForm";
import { CheckboxList } from "./CheckboxList";
import { fieldLabel } from "./fieldLabel";
import {
  clusterGroupFamily,
  isDefaultGroup,
  parameterGroupFamilyOf,
  restoredGroupFamily,
} from "./parameterGroupFamily";
import { useApiGet } from "./useApiGet";
import { STALE_CHOICE_NOTE, useStaleChoice } from "./useStaleChoice";

function describe(record: ResourceRecord, showKind: boolean): string {
  const facts = record.facts ?? {};
  if (facts.group_scope) {
    return `${record.identifier} (${String(facts.group_scope)}, ${String(facts.parameter_group_family)})`;
  }
  return showKind ? `${record.identifier} (${record.kind.replace("_", " ")})` : record.identifier;
}

/**
 * The parameter groups that can be attached to `resource`: those of the scope it takes (`cluster` for a
 * cluster, `instance` otherwise) and of the family of the group it has attached now. The attached group is
 * found by name among `all`; when it is not there, only the scope can be checked.
 *
 * @example
 * attachableGroups(groups, instanceRecord) // groups of the instance's current family, instance scope
 */
export function attachableGroups(
  candidates: ResourceRecord[],
  all: ResourceRecord[],
  resource: ResourceRecord,
) {
  const scope = resource.kind === "cluster" ? "cluster" : "instance";
  const attachedName =
    resource.relationships?.[
      resource.kind === "cluster" ? "cluster_parameter_group_name" : "parameter_group_name"
    ];
  const attached = all.find(
    (group) => group.identifier === attachedName && group.facts?.group_scope === scope,
  );
  const family = attached?.facts?.parameter_group_family;
  return candidates.filter(
    (group) =>
      group.facts?.group_scope === scope &&
      (family === undefined || group.facts?.parameter_group_family === family),
  );
}

/** Subnet groups have no resource page, so the picker lists them from the environment's inventory. */
const SUBNET_GROUP_KIND = "subnet_group";

/**
 * The subnet groups of every target in `inventory` whose ownership `ownership` allows, as picker options.
 *
 * @example
 * subnetGroupOptions(inventory, ["unmanaged"]); // [{ value: "arn:...:subgrp:private", label: "private (subnet group)" }]
 */
function subnetGroupOptions(
  inventory: EnvironmentInventory | null,
  ownership: string[] | undefined,
): PickerOption[] {
  return (inventory?.targets ?? [])
    .flatMap((target) => target.subnet_groups)
    .filter((group) => !ownership || ownership.includes(group.ownership))
    .map((group) => ({
      value: group.db_subnet_group_arn,
      label: `${group.db_subnet_group_name} (subnet group)`,
    }));
}

/** One choice of the picker; `disabled` choices are shown but cannot be picked. */
interface PickerOption {
  value: string;
  label: string;
  disabled?: boolean;
}

/** The records a narrowing hint leaves, and a sentence for the person when it leaves nothing yet. */
interface Narrowed {
  listed: ResourceRecord[];
  note: string | null;
}

/** The name of the form's `engine_version` field and the engine its hint names, when the form has one. */
function chosenVersionField(formSchema: ObjectSchema | undefined): { name: string; engine: unknown } | null {
  const entry = Object.entries(formSchema?.properties ?? {}).find(
    ([, property]) => property.ui?.widget === "engine_version",
  );
  return entry ? { name: entry[0], engine: entry[1].ui?.engine } : null;
}

/**
 * The records the picker lists once the narrowing hints are applied. Hints that depend on the task's resource
 * list nothing until its page has loaded; the chosen-version hint lists nothing until a version is chosen, and
 * takes the engine from the version field's hint (a create) or else from the task's resource (an upgrade).
 */
function narrow(
  matching: ResourceRecord[],
  allRecords: ResourceRecord[],
  ui: UiHints,
  resource: ResourceRecord | null,
  context: WidgetProps["context"],
  formValue: Record<string, unknown>,
  formSchema: ObjectSchema | undefined,
): Narrowed {
  let listed = ui.group_scope
    ? matching.filter((record) => record.facts?.group_scope === ui.group_scope)
    : matching;
  if (ui.members_of_resource) {
    listed = listed.filter(
      (record) =>
        context.resourceIdentifier !== undefined &&
        record.relationships?.cluster_identifier === context.resourceIdentifier,
    );
  }
  if (ui.same_family_and_scope_as_resource && context.resourceId) {
    listed = resource ? attachableGroups(listed, allRecords, resource) : [];
  }
  if (ui.instance_group_of_cluster_family && context.resourceId) {
    const family = resource ? clusterGroupFamily(resource, allRecords) : null;
    listed = listed.filter(
      (group) => group.facts?.group_scope === "instance" && group.facts?.parameter_group_family === family,
    );
  }
  if (ui.group_of_restored_family && context.resourceId) {
    const restored = resource ? restoredGroupFamily(resource) : null;
    listed = listed.filter(
      (group) =>
        restored !== null &&
        group.facts?.group_scope === restored.scope &&
        (restored.family === null || group.facts?.parameter_group_family === restored.family),
    );
  }
  if (ui.group_of_chosen_version_family) {
    const versionField = chosenVersionField(formSchema);
    // A create names its engine on the version field; an upgrade's engine is that of the task's resource.
    const engine = typeof versionField?.engine === "string" ? versionField.engine : resource?.facts?.engine;
    const family = versionField ? parameterGroupFamilyOf(engine, formValue[versionField.name]) : null;
    if (family === null) return { listed: [], note: "Choose an engine version first." };
    listed = listed.filter(
      (group) => group.facts?.parameter_group_family === family && !isDefaultGroup(group),
    );
  }
  return { listed, note: null };
}

/** A cluster member as a choice: its identifier, its role, and the current writer greyed out. */
function memberOption(record: ResourceRecord): PickerOption {
  const isWriter = record.facts?.is_cluster_writer === true;
  return {
    value: record.identifier,
    label: `${record.identifier} (${isWriter ? "current writer" : "reader"})`,
    disabled: isWriter,
  };
}

/**
 * Resources of the environment, by ARN. A select for one resource of `ui.resource_kind`; checkboxes when the
 * property is a list, narrowed to `ui.kinds` and `ui.ownership` when the hints give them (`subnet_group` in
 * `ui.kinds` adds the subnet groups of the environment's inventory, which adoption needs first). Further hints
 * narrow the list (see `UiHints`): `same_family_and_scope_as_resource` (see `attachableGroups`),
 * `instance_group_of_cluster_family`, `group_of_restored_family`, `group_scope` and
 * `group_of_chosen_version_family`. A choice the list offered and no longer offers (another engine version was
 * chosen) is cleared, with a note to pick again. With `members_of_resource` it lists the members of the task's cluster by
 * identifier, the writer shown but not selectable, and an empty choice lets AWS choose.
 */
export function ResourcePicker({
  name,
  schema,
  ui,
  value,
  onChange,
  problem,
  context,
  describedBy,
  formValue,
  formSchema,
}: WidgetProps) {
  const staleNoteId = useId();
  const resources = useApiGet<ResourceList>(
    context.environmentSlug ? `/api/v1/environments/${context.environmentSlug}/resources` : null,
    { kind: ui.resource_kind },
  );
  const wantsSubnetGroups = ui.kinds?.includes(SUBNET_GROUP_KIND) === true;
  const inventory = useApiGet<EnvironmentInventory>(
    wantsSubnetGroups && context.environmentSlug
      ? `/api/v1/environments/${context.environmentSlug}/inventory`
      : null,
  );
  const needsResourcePage =
    Boolean(context.resourceId) &&
    (ui.same_family_and_scope_as_resource === true ||
      ui.instance_group_of_cluster_family === true ||
      ui.group_of_restored_family === true ||
      (ui.group_of_chosen_version_family === true &&
        typeof chosenVersionField(formSchema)?.engine !== "string"));
  const resourcePage = useApiGet<ResourcePage>(
    needsResourcePage ? `/api/v1/resources/${context.resourceId}` : null,
  );
  const label = fieldLabel(name, schema, ui);
  const allRecords = resources.data?.resources ?? [];
  const matching = allRecords.filter(
    (record) =>
      (!ui.kinds || ui.kinds.includes(record.kind)) &&
      (!ui.ownership || ui.ownership.includes(record.ownership)),
  );
  const { listed, note } = narrow(
    matching,
    allRecords,
    ui,
    resourcePage.data?.record ?? null,
    context,
    formValue,
    formSchema,
  );
  const showKind = (ui.kinds?.length ?? 0) > 1;
  const options: PickerOption[] = [
    ...(wantsSubnetGroups ? subnetGroupOptions(inventory.data, ui.ownership) : []),
    ...listed.map((record) =>
      ui.members_of_resource
        ? memberOption(record)
        : { value: record.arn, label: describe(record, showKind) },
    ),
  ];
  const chosen = typeof value === "string" ? value : "";
  const settled =
    resources.data !== null &&
    (!needsResourcePage || resourcePage.data !== null) &&
    (!wantsSubnetGroups || inventory.data !== null);
  const clear = useCallback(() => onChange(undefined), [onChange]);
  const showStaleNote = useStaleChoice(
    schema.type === "array" ? "" : chosen,
    options.some((option) => option.value === chosen),
    settled,
    clear,
  );
  if (schema.type === "array") {
    const selected = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    return (
      <>
        <CheckboxList
          legend={label}
          options={options}
          selected={selected}
          onChange={onChange}
          describedBy={describedBy}
          emptyMessage={
            resources.error
              ? `Could not list resources: ${resources.error}`
              : resources.data
                ? "No matching resources in the latest inventory."
                : "Loading…"
          }
        />
        {inventory.error && (
          <span className="block text-xs text-status-red">
            Could not list subnet groups: {inventory.error}
          </span>
        )}
      </>
    );
  }
  return (
    <label className="block space-y-1 text-sm">
      <span>{label}</span>
      <select
        aria-label={label}
        aria-invalid={problem ? true : undefined}
        aria-describedby={
          [describedBy, showStaleNote ? staleNoteId : undefined].filter(Boolean).join(" ") || undefined
        }
        value={chosen}
        onChange={(e) => onChange(e.target.value || null)}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2 font-mono"
      >
        <option value="">{ui.members_of_resource ? "Let AWS choose" : "Choose…"}</option>
        {chosen && !options.some((option) => option.value === chosen) && (
          <option value={chosen}>{chosen}</option>
        )}
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
      {showStaleNote && (
        <span id={staleNoteId} className="block text-xs text-status-amber">
          {STALE_CHOICE_NOTE}
        </span>
      )}
      {note && <span className="block text-xs text-muted">{note}</span>}
      {(resources.error ?? resourcePage.error) && (
        <span className="block text-xs text-status-red">
          Could not list resources: {resources.error ?? resourcePage.error}
        </span>
      )}
    </label>
  );
}
