import type { ResourceRecord } from "@/api/types";

/** Which kind of resource a parameter group is attached to. */
export type GroupScope = "instance" | "cluster";

/**
 * The parameter group family of an engine version: the engine followed by the major version (from PostgreSQL
 * 10 the first number, before that the first two), as AWS names the families.
 *
 * @param engine `postgres` or `aurora-postgresql`.
 * @param engineVersion A version such as `16.4`.
 * @returns The family, or `null` when either is missing.
 *
 * @example
 * parameterGroupFamilyOf("postgres", "16.4"); // "postgres16"
 * parameterGroupFamilyOf("aurora-postgresql", "15.8"); // "aurora-postgresql15"
 * parameterGroupFamilyOf("postgres", "9.6.24"); // "postgres9.6"
 */
export function parameterGroupFamilyOf(engine: unknown, engineVersion: unknown): string | null {
  if (typeof engine !== "string" || engine === "") return null;
  if (typeof engineVersion !== "string" || engineVersion === "") return null;
  const [major = "", minor] = engineVersion.split(".");
  if (major === "") return null;
  const majorPart = Number(major) < 10 && minor !== undefined ? `${major}.${minor}` : major;
  return `${engine}${majorPart}`;
}

/**
 * The family and scope of the groups a restore can attach: the snapshot's (its engine is in the describe
 * entry, its scope in `snapshot_scope`), or the source instance's or cluster's for a point-in-time restore.
 *
 * @example
 * restoredGroupFamily(clusterSnapshotRecord); // { scope: "cluster", family: "aurora-postgresql16" }
 */
export function restoredGroupFamily(record: ResourceRecord): { scope: GroupScope; family: string | null } {
  const facts = record.facts ?? {};
  if (record.kind === "snapshot") {
    const scope: GroupScope = facts.snapshot_scope === "cluster" ? "cluster" : "instance";
    const engine = facts.engine ?? record.raw?.Engine;
    return { scope, family: parameterGroupFamilyOf(engine, facts.engine_version) };
  }
  const scope: GroupScope = record.kind === "cluster" ? "cluster" : "instance";
  return { scope, family: parameterGroupFamilyOf(facts.engine, facts.engine_version) };
}

/**
 * The family of a cluster's groups: that of the cluster group it has attached, found by name among `groups`,
 * else the one its engine and version name.
 *
 * @example
 * clusterGroupFamily(demoAurora, groups); // "aurora-postgresql16"
 */
export function clusterGroupFamily(cluster: ResourceRecord, groups: ResourceRecord[]): string | null {
  const attachedName = cluster.relationships?.cluster_parameter_group_name;
  const attached = groups.find(
    (group) => group.identifier === attachedName && group.facts?.group_scope === "cluster",
  );
  const family = attached?.facts?.parameter_group_family;
  if (typeof family === "string" && family !== "") return family;
  return parameterGroupFamilyOf(cluster.facts?.engine, cluster.facts?.engine_version);
}

/** AWS's own groups (`default.postgres16`) cannot be changed, so a create must not be given one. */
export function isDefaultGroup(group: ResourceRecord): boolean {
  return group.identifier.startsWith("default.");
}
