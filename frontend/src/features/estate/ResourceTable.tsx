import { useNavigate } from "react-router";
import type { ResourceRecord } from "@/api/types";
import { type Column, DataTable } from "@/components/DataTable";
import { OwnershipBadge } from "@/components/OwnershipBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { names, type Scalar, scalar } from "@/lib/recordValues";

/** Identifiers, versions and other machine text render in monospace; an absent value is a dash. */
function mono(value: Scalar) {
  return <span className="font-mono">{value === null ? "—" : String(value)}</span>;
}

const identifierColumn: Column<ResourceRecord> = {
  key: "identifier",
  header: "Identifier",
  sortValue: (record) => record.identifier,
  render: (record) => <span className="font-mono font-medium">{record.identifier}</span>,
};

const statusColumn: Column<ResourceRecord> = {
  key: "status",
  header: "Status",
  sortValue: (record) => record.status,
  render: (record) => <StatusBadge status={record.status} />,
};

const ownershipColumn: Column<ResourceRecord> = {
  key: "ownership",
  header: "Ownership",
  sortValue: (record) => record.ownership,
  render: (record) => <OwnershipBadge ownership={record.ownership} />,
};

const versionColumn: Column<ResourceRecord> = {
  key: "version",
  header: "Version",
  sortValue: (record) => String(scalar(record.facts, "engine_version") ?? ""),
  render: (record) => mono(scalar(record.facts, "engine_version")),
};

/** `yes` or `no`; null for a cluster member, whose availability comes from the cluster, not its own flag. */
function multiAzLabel(record: ResourceRecord): string | null {
  if (scalar(record.relationships, "cluster_identifier")) return null;
  return scalar(record.facts, "multi_az") === true ? "yes" : "no";
}

/** What an instance is to another resource: a member of a cluster, a replica of a source, or neither. */
function instanceRelationship(record: ResourceRecord): string | null {
  const cluster = scalar(record.relationships, "cluster_identifier");
  if (cluster) return `member of ${cluster}`;
  const source = scalar(record.relationships, "read_replica_source_identifier");
  return source ? `replica of ${source}` : null;
}

const instanceIdentifierColumn: Column<ResourceRecord> = {
  ...identifierColumn,
  render: (record) => {
    const relationship = instanceRelationship(record);
    return (
      <div className="flex flex-col leading-tight">
        <span className="font-mono font-medium">{record.identifier}</span>
        {relationship && <span className="font-mono text-xs text-muted">{relationship}</span>}
      </div>
    );
  },
};

const INSTANCE_COLUMNS: Column<ResourceRecord>[] = [
  instanceIdentifierColumn,
  statusColumn,
  versionColumn,
  {
    key: "class",
    header: "Class",
    sortValue: (record) => String(scalar(record.facts, "db_instance_class") ?? ""),
    render: (record) => mono(scalar(record.facts, "db_instance_class")),
  },
  {
    key: "multi_az",
    header: "Multi-AZ",
    sortValue: (record) => multiAzLabel(record) ?? "",
    render: (record) => multiAzLabel(record) ?? <span title="Aurora cluster member">—</span>,
  },
  {
    key: "group",
    header: "Parameter group",
    sortValue: (record) => String(scalar(record.relationships, "parameter_group_name") ?? ""),
    render: (record) => (
      <span className="inline-flex items-center gap-2">
        {mono(scalar(record.relationships, "parameter_group_name"))}
        {record.parameter_apply_status && record.parameter_apply_status !== "in-sync" && (
          <StatusBadge status={record.parameter_apply_status} />
        )}
      </span>
    ),
  },
  ownershipColumn,
];

const CLUSTER_COLUMNS: Column<ResourceRecord>[] = [
  identifierColumn,
  statusColumn,
  versionColumn,
  {
    key: "members",
    header: "Members",
    sortValue: (record) => names(record.relationships, "member_identifiers").length,
    render: (record) => String(names(record.relationships, "member_identifiers").length),
  },
  {
    key: "group",
    header: "Cluster group",
    sortValue: (record) => String(scalar(record.relationships, "cluster_parameter_group_name") ?? ""),
    render: (record) => mono(scalar(record.relationships, "cluster_parameter_group_name")),
  },
  ownershipColumn,
];

const PARAMETER_GROUP_COLUMNS: Column<ResourceRecord>[] = [
  identifierColumn,
  {
    key: "scope",
    header: "Scope",
    sortValue: (record) => String(scalar(record.facts, "group_scope") ?? ""),
    render: (record) => scalar(record.facts, "group_scope") ?? "—",
  },
  {
    key: "family",
    header: "Family",
    sortValue: (record) => String(scalar(record.facts, "parameter_group_family") ?? ""),
    render: (record) => mono(scalar(record.facts, "parameter_group_family")),
  },
  {
    key: "changed",
    header: "Changed",
    sortValue: (record) => Number(scalar(record.facts, "user_set_parameter_count") ?? 0),
    render: (record) => String(scalar(record.facts, "user_set_parameter_count") ?? 0),
  },
  {
    key: "used_by",
    header: "Used by",
    sortValue: (record) => names(record.facts, "used_by").length,
    render: (record) => mono(names(record.facts, "used_by").join(", ") || null),
  },
  ownershipColumn,
];

const SNAPSHOT_COLUMNS: Column<ResourceRecord>[] = [
  identifierColumn,
  {
    key: "source",
    header: "Source",
    sortValue: (record) => String(scalar(record.relationships, "source_identifier") ?? ""),
    render: (record) => mono(scalar(record.relationships, "source_identifier")),
  },
  {
    key: "type",
    header: "Type",
    sortValue: (record) => String(scalar(record.facts, "snapshot_type") ?? ""),
    render: (record) => scalar(record.facts, "snapshot_type") ?? "—",
  },
  statusColumn,
  versionColumn,
  {
    key: "size",
    header: "Size (GB)",
    sortValue: (record) => Number(scalar(record.facts, "allocated_storage_gb") ?? 0),
    render: (record) => scalar(record.facts, "allocated_storage_gb") ?? "—",
  },
  {
    key: "created",
    header: "Created",
    sortValue: (record) => String(scalar(record.facts, "created_at_source_utc") ?? ""),
    render: (record) => mono(scalar(record.facts, "created_at_source_utc")),
  },
];

const COLUMNS_BY_KIND: Record<string, Column<ResourceRecord>[]> = {
  instance: INSTANCE_COLUMNS,
  cluster: CLUSTER_COLUMNS,
  parameter_group: PARAMETER_GROUP_COLUMNS,
  snapshot: SNAPSHOT_COLUMNS,
};
const FALLBACK_COLUMNS = [identifierColumn, statusColumn, ownershipColumn];

/** What the filter box matches: identifier, status, ownership, facts and related names. */
function searchText(record: ResourceRecord): string {
  return [
    record.identifier,
    record.status,
    record.ownership,
    JSON.stringify(record.facts),
    JSON.stringify(record.relationships),
  ].join(" ");
}

/**
 * Dense list of one resource kind with kind-specific columns; Enter or a click opens the resource page.
 *
 * @example
 * <ResourceTable kind="instance" resources={instances} />
 */
export function ResourceTable({ kind, resources }: { kind: string; resources: ResourceRecord[] }) {
  const navigate = useNavigate();
  const columns = COLUMNS_BY_KIND[kind] ?? FALLBACK_COLUMNS;
  return (
    <DataTable
      columns={columns}
      rows={resources}
      rowKey={(record) => record.arn}
      onOpen={(record) => navigate(`/resources/${record.resource_id}`)}
      filterPlaceholder={`Filter ${kind.replace("_", " ")}s`}
      searchText={searchText}
    />
  );
}
