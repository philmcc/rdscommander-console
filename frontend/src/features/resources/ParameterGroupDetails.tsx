import { useMemo } from "react";
import { Link } from "react-router";
import { apiGet } from "@/api/client";
import type { FamilyMetadata, ResourceRecord } from "@/api/types";
import { ParameterTable, type ParameterTableRow } from "@/components/ParameterTable";
import { Button } from "@/components/ui/button";
import { names, scalar } from "@/lib/recordValues";
import { FactsGrid } from "./FactsGrid";
import { ResourceLinks } from "./ResourceLink";
import { ResourceSection } from "./ResourceSection";
import { useRelatedData } from "./useRelatedData";
import type { ResourceDirectory } from "./useResourceDirectory";

/** A parameter the group sets, as the inventory records it. */
interface SetParameter {
  value: string;
  apply_method: string;
}

/** What became of one override when the console built the group for another family. */
interface OverrideOutcome {
  name: string;
  outcome: string;
  note: string;
  /** The value the classification proposed; null when it proposed none. */
  value: string | null;
  /** The value the group was built with, after any edit on the review; null when none was set. */
  final_value: string | null;
}

/** How the console built a group for an upgrade. */
interface Provenance {
  source_group_names: string[];
  target_family: string;
  classification: OverrideOutcome[];
  operation_id: string | null;
}

function isRecordOf(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The parameters set on the group: `{name: {value, apply_method}}` in the record's facts. */
function readSetParameters(facts: Record<string, unknown>): Record<string, SetParameter> {
  const parameters: Record<string, SetParameter> = {};
  if (!isRecordOf(facts.parameters)) return parameters;
  for (const [name, entry] of Object.entries(facts.parameters)) {
    if (isRecordOf(entry) && typeof entry.value === "string") {
      parameters[name] = {
        value: entry.value,
        apply_method: typeof entry.apply_method === "string" ? entry.apply_method : "immediate",
      };
    }
  }
  return parameters;
}

/** The provenance in the record's facts; null for a group the console did not build. */
function readProvenance(facts: Record<string, unknown>): Provenance | null {
  const value = facts.provenance;
  if (!isRecordOf(value) || typeof value.target_family !== "string") return null;
  return {
    source_group_names: names(value, "source_group_names"),
    target_family: value.target_family,
    classification: Array.isArray(value.classification)
      ? value.classification.filter(isRecordOf).map((entry) => ({
          name: String(entry.name ?? ""),
          outcome: String(entry.outcome ?? ""),
          note: String(entry.note ?? ""),
          value: typeof entry.value === "string" ? entry.value : null,
          final_value: typeof entry.final_value === "string" ? entry.final_value : null,
        }))
      : [],
    operation_id: typeof value.operation_id === "string" ? value.operation_id : null,
  };
}

/** Rows for the parameter table: every parameter of the family when known, else only the ones set here. */
function parameterRows(
  family: FamilyMetadata | null,
  current: Record<string, SetParameter>,
): ParameterTableRow[] {
  if (!family) {
    return Object.entries(current).map(([name, set]) => ({
      name,
      value: set.value,
      default: null,
      data_type: "",
      apply_type: set.apply_method === "pending-reboot" ? "static" : "dynamic",
      is_modifiable: true,
      description: "",
      changed: true,
    }));
  }
  const known = Object.values(family.parameters)
    .sort((first, second) => first.name.localeCompare(second.name))
    .map((metadata) => ({
      name: metadata.name,
      value: current[metadata.name]?.value ?? null,
      default: metadata.default_value,
      data_type: metadata.data_type,
      apply_type: metadata.apply_type,
      is_modifiable: metadata.is_modifiable,
      description: metadata.description,
      changed: metadata.name in current,
    }));
  // A parameter set here that the family no longer lists is still part of the group.
  const unknown = Object.entries(current)
    .filter(([name]) => !(name in family.parameters))
    .map(([name, set]) => ({
      name,
      value: set.value,
      default: null,
      data_type: "",
      apply_type: set.apply_method === "pending-reboot" ? "static" : "dynamic",
      is_modifiable: true,
      description: "",
      changed: true,
    }));
  return [...known, ...unknown];
}

/**
 * What the provenance says about one override: its outcome when it was not simply carried, or the value the
 * operator set on the review when it differs from the carried one; null for an override carried unchanged.
 *
 * @example
 * provenanceNote({ name: "work_mem", outcome: "carried", note: "", value: "1000", final_value: "2000" });
 * // "work_mem: set to 2000 on review (carried value 1000)"
 */
function provenanceNote(entry: OverrideOutcome): string | null {
  const edited = entry.final_value !== null && entry.final_value !== entry.value;
  const parts = [
    ...(entry.outcome !== "carried" ? [`${entry.outcome} (${entry.note})`] : []),
    ...(edited
      ? [`set to ${entry.final_value} on review (${entry.outcome} value ${entry.value ?? "none"})`]
      : []),
  ];
  return parts.length > 0 ? `${entry.name}: ${parts.join(", ")}` : null;
}

interface ParameterGroupDetailsProps {
  record: ResourceRecord;
  directory: ResourceDirectory;
}

/**
 * The body of a parameter group page: family, scope, who uses it, compare controls, how the console built
 * it when it did, and the parameters table with its search and "changed from default" filter.
 *
 * @example
 * <ParameterGroupDetails record={page.record} directory={directory} />
 */
export function ParameterGroupDetails({ record, directory }: ParameterGroupDetailsProps) {
  const facts = record.facts ?? {};
  const familyName = String(scalar(facts, "parameter_group_family") ?? "");
  const scope = scalar(facts, "group_scope") === "cluster" ? "cluster" : "instance";
  // An Aurora cluster group has the family's cluster-scope parameters, not its instance-scope ones.
  const { data: family, error: familyError } = useRelatedData(
    `${record.environment_slug}/${familyName}/${scope}`,
    () =>
      apiGet<FamilyMetadata>(`/api/v1/parameters/families/${familyName}`, {
        environment_slug: record.environment_slug,
        scope,
      }),
  );
  const current = useMemo(() => readSetParameters(facts), [facts]);
  const rows = useMemo(() => parameterRows(family, current), [family, current]);
  const provenance = readProvenance(facts);
  const usedBy = names(facts, "used_by");
  const pendingReboot = names(facts, "pending_reboot_instances");
  const findUser = (identifier: string) => directory.find(scope, identifier);
  const provenanceNotes = (provenance?.classification ?? []).flatMap((entry) => provenanceNote(entry) ?? []);
  return (
    <div className="space-y-4">
      <ResourceSection title="Parameter group">
        <FactsGrid
          facts={[
            { label: "Family", value: familyName },
            { label: "Scope", value: scope },
            { label: "Description", value: facts.description, mono: false },
            { label: "Default group", value: scalar(facts, "is_default") },
            {
              label: "Used by",
              value: usedBy.length > 0 ? <ResourceLinks identifiers={usedBy} find={findUser} /> : "nothing",
            },
            ...(pendingReboot.length > 0
              ? [
                  {
                    label: "Waiting for a reboot",
                    value: <ResourceLinks identifiers={pendingReboot} find={findUser} />,
                  },
                ]
              : []),
          ]}
        />
        <div className="mt-3 flex flex-wrap gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to={`/estate/parameter-groups?compare=${record.resource_id}&defaults=1`}>
              Compare with defaults
            </Link>
          </Button>
          <Button variant="outline" size="sm" asChild>
            <Link to={`/estate/parameter-groups?compare=${record.resource_id}`}>
              Compare with another group
            </Link>
          </Button>
        </div>
      </ResourceSection>
      {provenance && (
        <ResourceSection title={`Built by the console for ${provenance.target_family}`}>
          <p className="text-sm text-muted">
            From <span className="font-mono">{provenance.source_group_names.join(", ")}</span>.{" "}
            {provenanceNotes.length > 0 ? provenanceNotes.join("; ") : "Every override carried unchanged."}
          </p>
          {provenance.operation_id && (
            <p className="mt-1 text-sm">
              <Link to={`/operations/${provenance.operation_id}`} className="text-accent hover:underline">
                Open the operation that built it
              </Link>
            </p>
          )}
        </ResourceSection>
      )}
      <ResourceSection title="Parameters">
        {familyError && (
          <p role="status" className="mb-2 text-sm text-muted">
            Defaults and descriptions could not be loaded ({familyError}); only the values set on this group
            are shown.
          </p>
        )}
        {!family && !familyError && (
          <p className="mb-2 text-sm text-muted">Loading defaults and descriptions…</p>
        )}
        <ParameterTable rows={rows} />
      </ResourceSection>
    </div>
  );
}
