import { useMemo } from "react";
import type { FamilyMetadata, ResourcePage } from "@/api/types";
import { type CurrentParameter, ParameterEditor } from "@/features/parameters/ParameterEditor";
import type { WidgetProps } from "../SchemaForm";
import { useApiGet } from "./useApiGet";

const NAME_CHARACTER = /[A-Za-z0-9_.-]/;
/** The form field that lists the parameters to return to their default; the task's options name it so. */
const RESET_FIELD = "reset";
const NO_EDITS: Record<string, string> = {};
const NO_PARAMETERS: Record<string, CurrentParameter> = {};

/**
 * Attribute server messages that do not name a field to the parameter they start with, for example
 * `work_mem must be an integer` to `work_mem`. A message belongs to the longest name it starts with, and
 * only when the name ends there (so `work_mem` does not claim `work_mem_extra ...`).
 *
 * @example
 * parameterProblemsByName(["work_mem must be an integer"], ["work_mem"]) // { work_mem: "work_mem must be an integer" }
 */
export function parameterProblemsByName(messages: string[], names: string[]): Record<string, string> {
  const byName: Record<string, string> = {};
  for (const message of messages) {
    let owner: string | null = null;
    for (const name of names) {
      const ends = message.length === name.length || !NAME_CHARACTER.test(message.charAt(name.length));
      if (message.startsWith(name) && ends && (owner === null || name.length > owner.length)) owner = name;
    }
    if (owner !== null) byName[owner] = byName[owner] ? `${byName[owner]}; ${message}` : message;
  }
  return byName;
}

/** The parameters the group sets, from the facts of its resource page: `{name: {value, apply_method}}`. */
function readCurrentParameters(facts: unknown): Record<string, CurrentParameter> {
  if (typeof facts !== "object" || facts === null) return NO_PARAMETERS;
  const current: Record<string, CurrentParameter> = {};
  for (const [name, entry] of Object.entries(facts)) {
    if (typeof entry === "object" && entry !== null && "value" in entry && typeof entry.value === "string") {
      current[name] = {
        value: entry.value,
        apply_method:
          "apply_method" in entry && typeof entry.apply_method === "string" ? entry.apply_method : undefined,
      };
    }
  }
  return current;
}

/**
 * Parameter editor bound to `{ name: value }` edits. The family comes from the resource the task runs on,
 * or from the form's `family` field when creating a group; its metadata is read for the group's scope (the
 * resource's `group_scope`, or the form's `scope`), so a cluster group is checked against cluster parameters. Where the form has a `reset` list (editing an
 * existing group), the editor can also queue parameters to return to their default, and keeps that list in
 * step with the edits through `onFormChange`.
 */
export function ParameterTableField({
  name,
  value,
  onChange,
  onFormChange,
  context,
  formValue,
  generalProblems,
}: WidgetProps) {
  const edits = (value ?? NO_EDITS) as Record<string, string>;
  const resourcePage = useApiGet<ResourcePage>(
    context.resourceId ? `/api/v1/resources/${context.resourceId}` : null,
  );
  const facts = resourcePage.data?.record.facts;
  const familyName = context.resourceId
    ? typeof facts?.parameter_group_family === "string"
      ? facts.parameter_group_family
      : null
    : typeof formValue.family === "string" && formValue.family
      ? formValue.family
      : null;
  // An Aurora cluster group has the family's cluster-scope parameters: the group's own scope when editing
  // it, the form's scope when creating one.
  const scope =
    (context.resourceId ? facts?.group_scope : formValue.scope) === "cluster" ? "cluster" : "instance";
  const setParameters = facts?.parameters;
  const current = useMemo(() => readCurrentParameters(setParameters), [setParameters]);
  const family = useApiGet<FamilyMetadata>(
    context.environmentSlug && familyName ? `/api/v1/parameters/families/${familyName}` : null,
    { environment_slug: context.environmentSlug, scope },
  );
  const queuedResets = formValue[RESET_FIELD];
  const resets = useMemo(
    () =>
      onFormChange
        ? Array.isArray(queuedResets)
          ? queuedResets.filter((entry): entry is string => typeof entry === "string")
          : []
        : undefined,
    [onFormChange, queuedResets],
  );
  if (!family.data) {
    const reason = resourcePage.error ?? family.error;
    return (
      <p className="text-sm text-muted">
        {reason
          ? `Parameters${familyName ? ` of ${familyName}` : ""} are not available: ${reason}`
          : familyName
            ? "Loading parameters…"
            : "Choose a family to edit parameters."}
      </p>
    );
  }
  return (
    <ParameterEditor
      family={family.data}
      current={current}
      edits={edits}
      resets={resets}
      problems={parameterProblemsByName(generalProblems ?? [], Object.keys(family.data.parameters))}
      onChange={(nextEdits, nextResets) => {
        if (nextResets !== undefined && onFormChange) {
          onFormChange({ [name]: nextEdits, [RESET_FIELD]: nextResets });
        } else {
          onChange(nextEdits);
        }
      }}
    />
  );
}
