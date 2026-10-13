import { X } from "lucide-react";
import { type KeyboardEvent, useId, useState } from "react";
import type { ResourceList, ResourceRecord } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WidgetProps } from "../SchemaForm";
import { fieldLabel } from "./fieldLabel";
import { useApiGet } from "./useApiGet";

/** The shape AWS gives a VPC security group id; the backend's `ui.item_pattern` replaces it when given. */
const DEFAULT_SECURITY_GROUP_PATTERN = "^sg-[0-9a-f]{8,17}$";

/**
 * Why `candidate` cannot be added to `listed`, or `null` when it can.
 *
 * @param candidate The id typed, trimmed.
 * @param listed The ids already chosen.
 * @param pattern The regular expression every id must match.
 * @returns A sentence for the person, or `null`.
 *
 * @example
 * securityGroupProblem("notagroup", [], "^sg-[0-9a-f]{8,17}$"); // "A security group id must look like sg-0123456789abcdef0."
 * securityGroupProblem("sg-0123456789abcdef0", [], "^sg-[0-9a-f]{8,17}$"); // null
 */
export function securityGroupProblem(candidate: string, listed: string[], pattern: string): string | null {
  if (!new RegExp(pattern).test(candidate)) return "A security group id must look like sg-0123456789abcdef0.";
  if (listed.includes(candidate)) return `${candidate} is already in the list.`;
  return null;
}

/**
 * The security groups the databases of the inventory use, each with the databases that use it, read from the
 * describe entries (`VpcSecurityGroups`), sorted by id.
 *
 * @example
 * securityGroupsInUse([demoOrders]); // [{ groupId: "sg-0aaa…", usedBy: ["demo-orders"] }]
 */
export function securityGroupsInUse(records: ResourceRecord[]): { groupId: string; usedBy: string[] }[] {
  const usedBy = new Map<string, string[]>();
  for (const record of records) {
    const groups = record.raw?.VpcSecurityGroups;
    if (!Array.isArray(groups)) continue;
    for (const entry of groups) {
      const groupId =
        entry !== null && typeof entry === "object"
          ? (entry as Record<string, unknown>).VpcSecurityGroupId
          : null;
      if (typeof groupId !== "string") continue;
      const names = usedBy.get(groupId) ?? [];
      if (!names.includes(record.identifier)) usedBy.set(groupId, [...names, record.identifier]);
    }
  }
  return [...usedBy.entries()]
    .map(([groupId, names]) => ({ groupId, usedBy: names }))
    .sort((first, second) => first.groupId.localeCompare(second.groupId));
}

/** Kinds of resource whose describe entries name security groups. */
const DATABASE_KINDS = new Set(["instance", "cluster"]);

/**
 * VPC security group ids as a list: type an id and press Add (or Enter); each chosen id has a Remove button.
 * An id that does not match `ui.item_pattern`, or is already listed, is refused beside the input. The groups
 * the target's databases already use are offered as one-click suggestions (the target chosen in the form, else
 * the task's). The value is the list of ids.
 *
 * @example
 * <SecurityGroupsField name="vpc_security_group_ids" schema={{ type: "array" }} ui={{ widget: "security_groups" }} value={[]} onChange={setIds} context={{ environmentSlug: "sandbox" }} formValue={{}} />
 */
export function SecurityGroupsField({
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
  const draftProblemId = useId();
  const [draft, setDraft] = useState("");
  const [draftProblem, setDraftProblem] = useState<string | null>(null);
  const listed = Array.isArray(value) ? value.filter((id): id is string => typeof id === "string") : [];
  const pattern = ui.item_pattern ?? DEFAULT_SECURITY_GROUP_PATTERN;
  const resources = useApiGet<ResourceList>(
    context.environmentSlug ? `/api/v1/environments/${context.environmentSlug}/resources` : null,
  );
  const targetKey = typeof formValue.target_key === "string" ? formValue.target_key : context.targetKey;
  const suggestions = securityGroupsInUse(
    (resources.data?.resources ?? []).filter(
      (record) => DATABASE_KINDS.has(record.kind) && (!targetKey || record.target_key === targetKey),
    ),
  ).filter((suggestion) => !listed.includes(suggestion.groupId));
  const label = fieldLabel(name, schema, ui);

  const add = () => {
    const candidate = draft.trim();
    const refusal = securityGroupProblem(candidate, listed, pattern);
    setDraftProblem(refusal);
    if (refusal !== null) return;
    onChange([...listed, candidate]);
    setDraft("");
  };
  const addOnEnter = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    add();
  };
  return (
    <fieldset className="min-w-0 space-y-2 text-sm" aria-describedby={describedBy}>
      <legend className="mb-1">{label}</legend>
      {listed.length > 0 && (
        <ul aria-label={`${label} chosen`} className="flex flex-wrap gap-1.5">
          {listed.map((groupId) => (
            <li
              key={groupId}
              className="inline-flex items-center gap-1 rounded-md border border-hairline bg-paper py-0.5 pr-0.5 pl-2 font-mono text-xs"
            >
              {groupId}
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                className="size-5"
                aria-label={`Remove ${groupId}`}
                onClick={() => onChange(listed.filter((other) => other !== groupId))}
              >
                <X aria-hidden="true" className="size-3" />
              </Button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center gap-2">
        <label htmlFor={inputId} className="sr-only">
          Security group id
        </label>
        <Input
          id={inputId}
          placeholder="sg-0123456789abcdef0"
          aria-invalid={draftProblem || problem ? true : undefined}
          aria-describedby={
            [describedBy, draftProblem ? draftProblemId : undefined].filter(Boolean).join(" ") || undefined
          }
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setDraftProblem(null);
          }}
          onKeyDown={addOnEnter}
          autoComplete="off"
          className="max-w-64 font-mono"
        />
        <Button type="button" variant="outline" size="sm" onClick={add}>
          Add
        </Button>
      </div>
      {draftProblem && (
        <p id={draftProblemId} className="text-xs text-status-red">
          {draftProblem}
        </p>
      )}
      {suggestions.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs text-muted">In use by databases in this account and region:</p>
          <div className="flex flex-wrap gap-1.5">
            {suggestions.map((suggestion) => (
              <Button
                key={suggestion.groupId}
                type="button"
                variant="outline"
                size="sm"
                className="h-auto py-1 text-xs"
                aria-label={`Add ${suggestion.groupId}, used by ${suggestion.usedBy.join(", ")}`}
                onClick={() => onChange([...listed, suggestion.groupId])}
              >
                <span className="font-mono">+ {suggestion.groupId}</span>
                <span className="text-muted">{suggestion.usedBy.join(", ")}</span>
              </Button>
            ))}
          </div>
        </div>
      )}
    </fieldset>
  );
}
