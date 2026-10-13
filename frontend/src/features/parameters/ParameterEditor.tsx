import { useMemo } from "react";
import type { FamilyMetadata } from "@/api/types";
import { ParameterTable, type ParameterTableRow } from "@/components/ParameterTable";
import { validateParameterValue } from "./validation";

/** A parameter the group sets now, as the inventory records it. */
export interface CurrentParameter {
  value: string;
  apply_method?: string;
}

interface ParameterEditorProps {
  family: FamilyMetadata;
  /** The parameters the group sets now, by name; every other parameter is at the family default. */
  current: Record<string, CurrentParameter>;
  /** Pending edits: parameter name to the value typed for it. */
  edits: Record<string, string>;
  /**
   * Called with the edits after each change. When the change also touched the pending resets, which happens
   * when a reset is queued or cancelled or when a value is typed for a parameter that was queued, the new
   * list of resets comes as the second argument (apply both in one update).
   */
  onChange: (edits: Record<string, string>, resets?: string[]) => void;
  /**
   * Parameters queued to return to the family default. Pass it to offer "Reset to default" on rows whose
   * current value is set; leave it out where a reset makes no sense (a group that does not exist yet).
   */
  resets?: readonly string[];
  /** Messages the server gave for parameters, shown when the value itself passes the local checks. */
  problems?: Record<string, string>;
}

const NO_RESETS: readonly string[] = [];
const NO_PROBLEMS: Record<string, string> = {};

/** `record` without `name`. */
function without<T>(record: Record<string, T>, name: string): Record<string, T> {
  const { [name]: _dropped, ...rest } = record;
  return rest;
}

/** The summary under the table: what changes, what resets, and what needs a reboot. */
function summarise(changed: number, reset: number, reboots: number): string {
  const parts = [`${changed} parameter${changed === 1 ? "" : "s"} change${changed === 1 ? "s" : ""}`];
  if (reset > 0) parts.push(`${reset} reset to default`);
  parts.push(`${reboots} need${reboots === 1 ? "s" : ""} a reboot`);
  return parts.join(", ");
}

/**
 * Editable table of every parameter of a family. Typed values are validated against the family's metadata
 * with the backend's own rules (ranges, choices, booleans, non-modifiable parameters; RDS formulas are
 * accepted), a parameter the group sets can be queued to return to its default, and the footer sums the
 * change: how many parameters change, how many reset and how many need a reboot (static parameters).
 *
 * @example
 * <ParameterEditor family={family} current={{ work_mem: { value: "16384" } }} edits={edits} onChange={setEdits} />
 */
export function ParameterEditor({
  family,
  current,
  edits,
  onChange,
  resets,
  problems: serverProblems = NO_PROBLEMS,
}: ParameterEditorProps) {
  const pendingResets = resets ?? NO_RESETS;
  const problems = useMemo(() => {
    const found: Record<string, string> = { ...serverProblems };
    for (const [name, value] of Object.entries(edits)) {
      const metadata = family.parameters[name];
      const message = metadata ? validateParameterValue(metadata, value) : null;
      if (message) found[name] = message;
    }
    return found;
  }, [edits, family, serverProblems]);
  const rows: ParameterTableRow[] = useMemo(
    () =>
      Object.values(family.parameters)
        .sort((first, second) => first.name.localeCompare(second.name))
        .map((metadata) => {
          const willReset = pendingResets.includes(metadata.name);
          const value = willReset
            ? metadata.default_value
            : (edits[metadata.name] ?? current[metadata.name]?.value ?? null);
          return {
            name: metadata.name,
            value,
            default: metadata.default_value,
            data_type: metadata.data_type,
            apply_type: metadata.apply_type,
            is_modifiable: metadata.is_modifiable,
            description: metadata.description,
            changed: value !== null && value !== metadata.default_value,
            edited: Object.hasOwn(edits, metadata.name),
            willReset,
            resettable: resets !== undefined && !willReset && Object.hasOwn(current, metadata.name),
          };
        }),
    [family, edits, current, pendingResets, resets],
  );

  const editValue = (name: string, value: string) => {
    const nextEdits = { ...edits, [name]: value };
    // A value typed for a parameter that was queued to reset replaces the reset.
    if (pendingResets.includes(name))
      onChange(
        nextEdits,
        pendingResets.filter((other) => other !== name),
      );
    else onChange(nextEdits);
  };

  const changed = Object.keys(edits).length;
  const rebooting = new Set([...Object.keys(edits), ...pendingResets]);
  const reboots = [...rebooting].filter((name) => family.parameters[name]?.apply_type === "static").length;
  return (
    <div className="space-y-2">
      <ParameterTable
        rows={rows}
        editable
        problems={problems}
        onChange={editValue}
        onReset={(name) => onChange(without(edits, name))}
        onResetToDefault={(name) => onChange(without(edits, name), [...pendingResets, name])}
        onUndoReset={(name) =>
          onChange(
            edits,
            pendingResets.filter((other) => other !== name),
          )
        }
      />
      <p className="text-sm text-muted">{summarise(changed, pendingResets.length, reboots)}</p>
    </div>
  );
}
