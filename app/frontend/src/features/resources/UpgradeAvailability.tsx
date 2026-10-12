import { Fragment } from "react";
import { apiGet } from "@/api/client";
import type { EngineVersionList, ResourceRecord } from "@/api/types";
import { newestVersionFirst } from "@/features/tasks/widgets/EngineVersionPicker";
import { scalar } from "@/lib/recordValues";
import { useRelatedData } from "./useRelatedData";

interface UpgradeTargetsProps {
  environmentSlug: string;
  engine: string;
  engineVersion: string;
}

/**
 * The versions the engine-versions table lists as upgrades of `engineVersion`, newest first, each marked
 * major or minor; nothing while they load, when there are none, or when the table cannot be read (it is a
 * hint, and the upgrade tasks list the same targets themselves).
 */
function UpgradeTargets({ environmentSlug, engine, engineVersion }: UpgradeTargetsProps) {
  const { data } = useRelatedData(`${environmentSlug}/${engine}`, () =>
    apiGet<EngineVersionList>("/api/v1/parameters/engine-versions", {
      environment_slug: environmentSlug,
      engine,
    }),
  );
  const targets = [
    ...(data?.versions.find((version) => version.engine_version === engineVersion)?.valid_upgrade_targets ??
      []),
  ].sort((first, second) => newestVersionFirst(first.engine_version, second.engine_version));
  if (targets.length === 0) return null;
  return (
    <p className="mt-2 text-sm">
      Upgrade available to{" "}
      {targets.map((target, index) => (
        <Fragment key={target.engine_version}>
          {index > 0 && ", "}
          <span className="font-mono">{target.engine_version}</span> ({target.is_major ? "major" : "minor"})
        </Fragment>
      ))}
    </p>
  );
}

/**
 * "Upgrade available to 17.2 (major), 16.6 (minor)" under the facts of a standalone instance or a cluster,
 * when the engine-versions table lists upgrade targets for its current version. An Aurora member says
 * nothing: it runs its cluster's version and is upgraded with the cluster.
 *
 * @example
 * <UpgradeAvailability record={page.record} />
 */
export function UpgradeAvailability({ record }: { record: ResourceRecord }) {
  const engine = scalar(record.facts, "engine");
  const engineVersion = scalar(record.facts, "engine_version");
  const isClusterMember =
    record.kind === "instance" && Boolean(scalar(record.relationships, "cluster_identifier"));
  if (typeof engine !== "string" || typeof engineVersion !== "string" || isClusterMember) return null;
  return (
    <UpgradeTargets environmentSlug={record.environment_slug} engine={engine} engineVersion={engineVersion} />
  );
}
