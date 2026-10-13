import { useId } from "react";
import { useSearchParams } from "react-router";
import type { ResourceRecord } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ParameterCompare } from "@/features/parameters/ParameterCompare";
import { ResourceSection } from "@/features/resources/ResourceSection";
import { EstateListPage } from "./EstateListPage";

/** The picker's value for "compare with the family defaults". */
const FAMILY_DEFAULTS = "defaults";

interface CompareSectionProps {
  /** Resource id of the group on the left. */
  leftResourceId: string;
  /** Resource id of the group on the right, from the address; empty when none is chosen. */
  rightResourceId: string;
  againstDefaults: boolean;
  /** The parameter groups of the environment loaded so far. */
  groups: ResourceRecord[];
  /** Called with a group's resource id, or `defaults`. */
  onChoose: (choice: string) => void;
  onClose: () => void;
}

/**
 * The comparison above the list of groups: a picker for the right-hand side (the family defaults or another
 * group of the same scope as the left one), a button to close it, and the table.
 */
function CompareSection({
  leftResourceId,
  rightResourceId,
  againstDefaults,
  groups,
  onChoose,
  onClose,
}: CompareSectionProps) {
  const pickerId = useId();
  const left = groups.find((group) => group.resource_id === leftResourceId);
  const scope = left?.facts?.group_scope;
  // A group is compared with groups of its own scope: an instance group with a cluster group has little in common.
  const candidates = groups.filter(
    (group) =>
      group.resource_id !== leftResourceId && (scope === undefined || group.facts?.group_scope === scope),
  );
  const chosen = againstDefaults ? FAMILY_DEFAULTS : rightResourceId;
  return (
    <ResourceSection title="Compare parameter groups" className="mb-4">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-1 text-sm">
          <label htmlFor={pickerId}>Compare with</label>
          <select
            id={pickerId}
            value={chosen}
            onChange={(event) => onChoose(event.target.value)}
            className="block h-8 min-w-64 rounded-md border border-hairline bg-surface px-2 font-mono"
          >
            <option value="">Choose a group…</option>
            <option value={FAMILY_DEFAULTS}>Family defaults</option>
            {chosen !== "" &&
              chosen !== FAMILY_DEFAULTS &&
              !candidates.some((group) => group.resource_id === chosen) && (
                <option value={chosen}>{chosen}</option>
              )}
            {candidates.map((group) => (
              <option key={group.resource_id} value={group.resource_id}>
                {group.identifier} ({String(group.facts?.parameter_group_family ?? "unknown family")})
              </option>
            ))}
          </select>
        </div>
        <Button variant="outline" size="sm" onClick={onClose}>
          Close comparison
        </Button>
      </div>
      {chosen === "" ? (
        <p className="text-sm text-muted">Choose a group, or the family defaults, to compare with.</p>
      ) : (
        <ParameterCompare
          leftResourceId={leftResourceId}
          rightResourceId={againstDefaults ? undefined : rightResourceId}
          againstDefaults={againstDefaults}
        />
      )}
    </ResourceSection>
  );
}

/**
 * The parameter groups of the selected environment. `?compare=<id>` opens a comparison of that group above
 * the list: with the family defaults for `&defaults=1`, with another group for `&with=<id>`; the picker in
 * the comparison changes the address, so a comparison can be linked to.
 *
 * @example
 * <Route path="estate/parameter-groups" element={<ParameterGroupsPage />} />
 */
export default function ParameterGroupsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const leftResourceId = searchParams.get("compare");
  const againstDefaults = ["1", "true"].includes(searchParams.get("defaults") ?? "");
  const rightResourceId = searchParams.get("with") ?? "";

  const choose = (choice: string) => {
    const next = new URLSearchParams(searchParams);
    next.delete("with");
    next.delete("defaults");
    if (choice === FAMILY_DEFAULTS) next.set("defaults", "1");
    else if (choice !== "") next.set("with", choice);
    setSearchParams(next, { replace: true });
  };
  const close = () => {
    const next = new URLSearchParams(searchParams);
    for (const key of ["compare", "with", "defaults"]) next.delete(key);
    setSearchParams(next, { replace: true });
  };
  return (
    <EstateListPage
      kind="parameter_group"
      title="Parameter groups"
      createKind="parameter_group_create"
      aboveList={
        leftResourceId
          ? (groups) => (
              <CompareSection
                leftResourceId={leftResourceId}
                rightResourceId={rightResourceId}
                againstDefaults={againstDefaults}
                groups={groups}
                onChoose={choose}
                onClose={close}
              />
            )
          : undefined
      }
    />
  );
}
