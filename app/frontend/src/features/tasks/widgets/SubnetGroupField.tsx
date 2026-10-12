import { useCallback, useId } from "react";
import type { EnvironmentInventory } from "@/api/types";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";
import { STALE_CHOICE_NOTE, useStaleChoice } from "./useStaleChoice";

/**
 * A DB subnet group of the environment, by name, from the environment's inventory: those of the target chosen
 * in the form (a create's `target_key`), else of the task's target, else of every target. Each choice shows
 * its VPC and how many subnets it spans, and the chosen group's description is shown beneath. A group chosen
 * for one target is cleared when another target is chosen, with a note to pick again.
 *
 * @example
 * <SubnetGroupField name="subnet_group_name" schema={{ type: "string" }} ui={{ widget: "subnet_group" }} value="" onChange={setName} context={{ environmentSlug: "sandbox" }} formValue={{}} />
 */
export function SubnetGroupField({
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
  const staleNoteId = useId();
  const inventory = useApiGet<EnvironmentInventory>(
    context.environmentSlug ? `/api/v1/environments/${context.environmentSlug}/inventory` : null,
  );
  const targetKey = typeof formValue.target_key === "string" ? formValue.target_key : context.targetKey;
  const groups = (inventory.data?.targets ?? [])
    .filter((target) => !targetKey || target.target_key === targetKey)
    .flatMap((target) => target.subnet_groups);
  const label = fieldLabel(name, schema, ui);
  const chosen = typeof value === "string" ? value : "";
  const chosenGroup = groups.find((group) => group.db_subnet_group_name === chosen);
  const clear = useCallback(() => onChange(undefined), [onChange]);
  const showStaleNote = useStaleChoice(chosen, chosenGroup !== undefined, inventory.data !== null, clear);
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
        onChange={(e) => onChange(e.target.value || undefined)}
        className="h-8 w-full rounded-md border border-hairline bg-surface px-2 font-mono"
      >
        <option value="">Choose…</option>
        {chosen && !chosenGroup && <option value={chosen}>{chosen}</option>}
        {groups.map((group) => (
          <option key={group.db_subnet_group_arn} value={group.db_subnet_group_name}>
            {group.db_subnet_group_name} ({group.vpc_id}, {group.subnet_ids.length}{" "}
            {group.subnet_ids.length === 1 ? "subnet" : "subnets"})
          </option>
        ))}
      </select>
      {showStaleNote && (
        <span id={staleNoteId} className="block text-xs text-status-amber">
          {STALE_CHOICE_NOTE}
        </span>
      )}
      {chosenGroup?.description && (
        <span className="block text-xs text-muted">{chosenGroup.description}</span>
      )}
      {inventory.data && groups.length === 0 && (
        <span className="block text-xs text-muted">
          No subnet groups in the latest inventory{targetKey ? " of this target" : ""}; refresh the inventory
          after creating one.
        </span>
      )}
      {inventory.error && (
        <span className="block text-xs text-status-red">Could not list subnet groups: {inventory.error}</span>
      )}
    </label>
  );
}
