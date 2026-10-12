import type { PendingMaintenanceView, ResourceRecord } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { names, scalar } from "@/lib/recordValues";
import { backupSummary } from "./backupSummary";
import { FactsGrid } from "./FactsGrid";
import { PendingMaintenance } from "./PendingMaintenance";
import { RelatedSnapshots } from "./RelatedSnapshots";
import { ResourceLink, ResourceLinks } from "./ResourceLink";
import { ResourceSection } from "./ResourceSection";
import { UpgradeAvailability } from "./UpgradeAvailability";
import type { ResourceDirectory } from "./useResourceDirectory";

/** `100 GB (autoscaling to 200 GB) gp3`; null when the size is unknown. */
function storageSummary(record: ResourceRecord): string | null {
  const allocated = scalar(record.facts, "allocated_storage_gb");
  if (allocated === null) return null;
  const maximum = scalar(record.facts, "max_allocated_storage_gb");
  return [
    `${allocated} GB`,
    maximum ? `(autoscaling to ${maximum} GB)` : null,
    scalar(record.facts, "storage_type"),
  ]
    .filter(Boolean)
    .join(" ");
}

/** Performance Insights and enhanced monitoring, in words. */
function monitoringSummary(record: ResourceRecord): string {
  const interval = scalar(record.facts, "monitoring_interval");
  return [
    scalar(record.facts, "performance_insights_enabled")
      ? "Performance Insights on"
      : "Performance Insights off",
    typeof interval === "number" && interval > 0
      ? `enhanced monitoring every ${interval} s`
      : "enhanced monitoring off",
  ].join(", ");
}

interface InstanceDetailsProps {
  record: ResourceRecord;
  directory: ResourceDirectory;
  pendingMaintenance: PendingMaintenanceView[];
}

/**
 * The body of an instance page: facts (with the engine upgrades available), parameter group with its apply
 * status, relationships, snapshots and pending maintenance.
 *
 * @example
 * <InstanceDetails record={page.record} directory={directory} pendingMaintenance={pending} />
 */
export function InstanceDetails({ record, directory, pendingMaintenance }: InstanceDetailsProps) {
  const facts = record.facts;
  const relationships = record.relationships;
  const groupName = scalar(relationships, "parameter_group_name");
  const clusterName = scalar(relationships, "cluster_identifier");
  const sourceName = scalar(relationships, "read_replica_source_identifier");
  return (
    <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2">
      <ResourceSection title="Facts">
        <FactsGrid
          facts={[
            {
              label: "Engine",
              value: `${scalar(facts, "engine") ?? "—"} ${scalar(facts, "engine_version") ?? ""}`.trim(),
            },
            { label: "Class", value: scalar(facts, "db_instance_class") },
            { label: "Storage", value: storageSummary(record) },
            { label: "Multi-AZ", value: scalar(facts, "multi_az") },
            {
              label: "Backups",
              value: backupSummary(
                scalar(facts, "backup_retention_period"),
                scalar(facts, "preferred_backup_window"),
              ),
            },
            { label: "Maintenance window", value: scalar(facts, "preferred_maintenance_window") },
            { label: "Monitoring", value: monitoringSummary(record), mono: false },
            { label: "Encrypted", value: scalar(facts, "storage_encrypted") },
            { label: "Deletion protection", value: scalar(facts, "deletion_protection") },
          ]}
        />
        <UpgradeAvailability record={record} />
      </ResourceSection>
      <ResourceSection title="Parameter group">
        <p className="flex flex-wrap items-center gap-2 text-sm">
          {groupName ? (
            <ResourceLink
              target={directory.find("parameter_group", String(groupName), "instance")}
              identifier={String(groupName)}
            />
          ) : (
            <span className="font-mono">—</span>
          )}
          {record.parameter_apply_status && <StatusBadge status={record.parameter_apply_status} />}
        </p>
      </ResourceSection>
      <ResourceSection title="Relationships">
        <FactsGrid
          facts={[
            {
              label: "Cluster",
              value: clusterName ? (
                <ResourceLink
                  target={directory.find("cluster", String(clusterName))}
                  identifier={String(clusterName)}
                />
              ) : null,
            },
            {
              label: "Replica of",
              value: sourceName ? (
                <ResourceLink
                  target={directory.find("instance", String(sourceName))}
                  identifier={String(sourceName)}
                />
              ) : null,
            },
            {
              label: "Replicas",
              value: (
                <ResourceLinks
                  identifiers={names(relationships, "replica_identifiers")}
                  find={(identifier) => directory.find("instance", identifier)}
                />
              ),
            },
          ]}
        />
      </ResourceSection>
      <PendingMaintenance pending={pendingMaintenance} />
      <RelatedSnapshots record={record} directory={directory} className="md:col-span-2" />
    </div>
  );
}
