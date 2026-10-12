import type { DefinitionSummary, ResourceRecord } from "@/api/types";
import type { ObjectSchema } from "@/features/tasks/SchemaForm";
import { isProtectedTagKey } from "@/features/tasks/widgets/TagsField";

/**
 * The resource's current values of `facts` named by `fieldsByFact` (`{fact: option}`), leaving out those the
 * inventory does not know.
 *
 * @example
 * currentValues(instance, { multi_az: "multi_az" }); // { multi_az: true }
 */
function currentValues(
  resource: ResourceRecord,
  fieldsByFact: Record<string, string>,
): Record<string, unknown> {
  const facts = (resource.facts ?? {}) as Record<string, unknown>;
  return Object.fromEntries(
    Object.entries(fieldsByFact)
      .filter(([fact]) => facts[fact] !== undefined && facts[fact] !== null)
      .map(([fact, option]) => [option, facts[fact]]),
  );
}

/**
 * The resource's AWS describe values named by `fieldsByRawKey` (`{DescribeKey: option}`), for settings the
 * inventory keeps only in `raw`, leaving out those AWS does not report.
 *
 * @example
 * rawValues(instance, { CopyTagsToSnapshot: "copy_tags_to_snapshot" }); // { copy_tags_to_snapshot: true }
 */
function rawValues(
  resource: ResourceRecord,
  fieldsByRawKey: Record<string, string>,
): Record<string, unknown> {
  const raw = (resource.raw ?? {}) as Record<string, unknown>;
  return Object.fromEntries(
    Object.entries(fieldsByRawKey)
      .filter(([rawKey]) => raw[rawKey] !== undefined && raw[rawKey] !== null)
      .map(([rawKey, option]) => [option, raw[rawKey]]),
  );
}

/**
 * The monitoring options of `resource`: Performance Insights, the enhanced monitoring interval and, while
 * enhanced monitoring is on, its role, which the backend needs whenever the interval is not 0.
 *
 * @example
 * monitoringValues(instance);
 * // { performance_insights_enabled: false, monitoring_interval_seconds: 60, monitoring_role_arn: "arn:aws:iam::..." }
 */
function monitoringValues(resource: ResourceRecord): Record<string, unknown> {
  const values = currentValues(resource, {
    performance_insights_enabled: "performance_insights_enabled",
    monitoring_interval: "monitoring_interval_seconds",
  });
  if (!values.monitoring_interval_seconds) return values;
  return { ...values, ...rawValues(resource, { MonitoringRoleArn: "monitoring_role_arn" }) };
}

/** Storage types the storage form offers; another type (magnetic `standard`) is not pre-filled. */
const STORAGE_TYPES_THE_FORM_OFFERS = new Set(["gp2", "gp3", "io1", "io2"]);

/**
 * The storage options of `resource`: size, autoscaling ceiling, type, IOPS and throughput as AWS reports
 * them, leaving out a storage type the form does not offer.
 *
 * @example
 * storageValues(instance); // { allocated_storage_gb: 100, storage_type: "gp3", iops: 3000, storage_throughput: 125 }
 */
function storageValues(resource: ResourceRecord): Record<string, unknown> {
  const values: Record<string, unknown> = {
    ...currentValues(resource, {
      allocated_storage_gb: "allocated_storage_gb",
      max_allocated_storage_gb: "max_allocated_storage_gb",
      storage_type: "storage_type",
      iops: "iops",
    }),
    ...rawValues(resource, { StorageThroughput: "storage_throughput" }),
  };
  if (!STORAGE_TYPES_THE_FORM_OFFERS.has(String(values.storage_type))) delete values.storage_type;
  return values;
}

/**
 * The tags of `resource` the tags form can change, as `set_tags`: console (`rdscommander:`) and AWS (`aws:`)
 * tags are left out, since the backend refuses them. Empty when no such tag is set.
 *
 * @example
 * tagValues(instance); // { set_tags: { team: "orders" } }
 */
function tagValues(resource: ResourceRecord): Record<string, unknown> {
  const tags = resource.facts?.tags;
  if (tags === null || typeof tags !== "object" || Array.isArray(tags)) return {};
  const changeableTags = Object.fromEntries(
    Object.entries(tags as Record<string, unknown>)
      .filter(([key]) => !isProtectedTagKey(key))
      .map(([key, value]) => [key, String(value)]),
  );
  return Object.keys(changeableTags).length > 0 ? { set_tags: changeableTags } : {};
}

/**
 * Options a task takes from the resource it is started on, beyond what its schema marks hidden. Adoption
 * selects the resource and the target it lives in. The settings forms start at the resource's current
 * values, so a switch shows what AWS has and a field the operator does not touch is sent as it is (the
 * backend leaves out of the change whatever already equals the live value).
 */
const OPTIONS_FROM_RESOURCE: Record<string, (resource: ResourceRecord) => Record<string, unknown>> = {
  terraform_adoption: (resource) => ({ target_key: resource.target_key, resource_arns: [resource.arn] }),
  multi_az_change: (resource) => currentValues(resource, { multi_az: "multi_az" }),
  deletion_protection_change: (resource) =>
    currentValues(resource, { deletion_protection: "deletion_protection" }),
  auto_minor_upgrade_change: (resource) =>
    currentValues(resource, { auto_minor_version_upgrade: "auto_minor_version_upgrade" }),
  monitoring_change: monitoringValues,
  // Tag copying is shown as a switch too, so the form starts from what AWS has rather than "off".
  backup_settings_change: (resource) => ({
    ...currentValues(resource, {
      backup_retention_period: "backup_retention_period_days",
      preferred_backup_window: "preferred_backup_window",
    }),
    ...rawValues(resource, { CopyTagsToSnapshot: "copy_tags_to_snapshot" }),
  }),
  maintenance_window_change: (resource) =>
    currentValues(resource, { preferred_maintenance_window: "preferred_maintenance_window" }),
  tags_change: tagValues,
  storage_change: storageValues,
  instance_class_change: (resource) => currentValues(resource, { db_instance_class: "instance_class" }),
  serverless_capacity_change: (resource) =>
    currentValues(resource, {
      serverless_v2_min_capacity: "min_capacity_acu",
      serverless_v2_max_capacity: "max_capacity_acu",
    }),
};

/**
 * The options a task of `kind` takes from `resource` by its kind alone; undefined when the kind takes none.
 * A task started on a resource (the launcher, or the Actions menu of a resource page) also needs the hidden
 * options of its definition, so it starts with `initialOptionsFor`, which includes these.
 *
 * @example
 * optionsFromResource("terraform_adoption", instance); // { target_key: "...", resource_arns: ["arn:..."] }
 */
export function optionsFromResource(
  kind: string,
  resource: ResourceRecord,
): Record<string, unknown> | undefined {
  return OPTIONS_FROM_RESOURCE[kind]?.(resource);
}

/** Names of the properties the form never shows, because the request that opens the task supplies them. */
function hiddenProperties(definition: DefinitionSummary): string[] {
  const properties = (definition.options_schema as ObjectSchema).properties ?? {};
  return Object.entries(properties)
    .filter(([, property]) => property.ui?.widget === "hidden")
    .map(([name]) => name);
}

/**
 * Whether the task cannot be started without a resource: its options hold a hidden `resource_arn`, which
 * only a chosen resource can fill in (drift resolution is one).
 *
 * @example
 * needsResource(driftResolveDefinition); // true
 */
export function needsResource(definition: DefinitionSummary): boolean {
  return hiddenProperties(definition).includes("resource_arn");
}

/**
 * The options a task starts with when it is started on `resource`: a hidden `resource_arn` is the
 * resource's ARN, plus whatever the task's kind takes from a resource (see `OPTIONS_FROM_RESOURCE`).
 *
 * @example
 * initialOptionsFor(driftResolveDefinition, instance); // { resource_arn: "arn:aws:rds:..." }
 */
export function initialOptionsFor(
  definition: DefinitionSummary,
  resource: ResourceRecord,
): Record<string, unknown> {
  return {
    ...(needsResource(definition) ? { resource_arn: resource.arn } : {}),
    ...optionsFromResource(definition.kind, resource),
  };
}

/**
 * How well a launcher entry matches what was typed; 0 hides it. A title that starts with the text ranks
 * first, then a title word that does, then the text anywhere in the title, then the text in the title with
 * its words run together ("failover" for "Fail over"), then the text in the entry's
 * group or summary, then every typed word somewhere in them. Letters scattered across unrelated words never
 * match, so Enter after typing a task's name starts that task.
 *
 * @example
 * launcherMatchScore("Reboot pending instances", "reboot"); // 1
 * launcherMatchScore("Adopt resources", "reboot", ["Bring existing resources under Terraform"]); // 0
 */
export function launcherMatchScore(value: string, search: string, keywords: string[] = []): number {
  const needle = search.trim().toLowerCase();
  if (!needle) return 1;
  const title = value.toLowerCase();
  if (title.startsWith(needle)) return 1;
  if (title.split(/[\s:/._-]+/).some((word) => word.startsWith(needle))) return 0.9;
  if (title.includes(needle)) return 0.8;
  // "failover" for "Fail over": the title's words run together, as the operator may type them.
  const joinedTitle = title.replace(/[\s:/._-]+/g, "");
  const joinedNeedle = needle.replace(/[\s:/._-]+/g, "");
  if (joinedNeedle && joinedTitle.includes(joinedNeedle)) return 0.7;
  const everything = [title, ...keywords.map((keyword) => keyword.toLowerCase())].join(" ");
  if (keywords.some((keyword) => keyword.toLowerCase().includes(needle))) return 0.5;
  if (needle.split(/\s+/).every((word) => everything.includes(word))) return 0.3;
  return 0;
}

/**
 * The words besides its title that a launcher entry matches on: its group and its summary.
 *
 * @example
 * launcherKeywords(adoptDefinition); // ["Discovery and adoption", "Bring existing resources under Terraform…"]
 */
export function launcherKeywords(definition: DefinitionSummary): string[] {
  return [definition.group, definition.summary];
}

/**
 * The launcher's groups in the order they show for `search`: by the best match among their entries (see
 * `launcherMatchScore`), so the entry that matches best comes first, and is selected, whichever group it is
 * in. Groups that match equally keep catalogue order, as every group does when nothing is typed. Entries
 * within a group are ranked by the command list itself.
 *
 * @example
 * orderGroupsByBestMatch([["Discovery and adoption", [adopt]], ["Environments", [importEnvironment]]], "import");
 * // [["Environments", [importEnvironment]], ["Discovery and adoption", [adopt]]]
 */
export function orderGroupsByBestMatch(
  groups: [string, DefinitionSummary[]][],
  search: string,
): [string, DefinitionSummary[]][] {
  const bestMatch = (definitions: DefinitionSummary[]) =>
    Math.max(
      0,
      ...definitions.map((definition) =>
        launcherMatchScore(definition.title, search, launcherKeywords(definition)),
      ),
    );
  return groups
    .map((group) => ({ group, score: bestMatch(group[1]) }))
    .sort((first, second) => second.score - first.score)
    .map(({ group }) => group);
}
