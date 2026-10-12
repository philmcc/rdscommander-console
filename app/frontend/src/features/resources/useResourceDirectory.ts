import { useMemo } from "react";
import { apiGet } from "@/api/client";
import type { ResourceList, ResourceRecord } from "@/api/types";
import { scalar } from "@/lib/recordValues";
import { useRelatedData } from "./useRelatedData";

/** The other resources of the environment, for the names a resource page can link to. */
export interface ResourceDirectory {
  /** The list has answered. */
  loaded: boolean;
  /** Why the list could not be read; `null` when it was, or has not answered yet. */
  error: string | null;
  /**
   * The resource of `kind` named `identifier` in the same target as the page's resource.
   *
   * @param groupScope Parameter groups of an instance and of a cluster may share a name; `instance` or
   *   `cluster` picks one.
   */
  find: (kind: string, identifier: string, groupScope?: string) => ResourceRecord | undefined;
  /** The snapshots taken of the instance or cluster `identifier` in the same target, newest first. */
  snapshotsOf: (scope: "instance" | "cluster", identifier: string) => ResourceRecord[];
}

/**
 * Every resource of the page's environment, read once for the inventory snapshot the record came from, so
 * a page can link a name to the page of the resource it names and list the snapshots of a database.
 * Until the list answers, `find` knows nothing and `snapshotsOf` is empty; the page works without it.
 *
 * @example
 * const directory = useResourceDirectory(page.record);
 * directory.find("cluster", "demo-aurora")?.arn;
 */
export function useResourceDirectory(record: ResourceRecord): ResourceDirectory {
  const { data, error } = useRelatedData(`${record.environment_slug}/${record.snapshot_id}`, () =>
    apiGet<ResourceList>(`/api/v1/environments/${record.environment_slug}/resources`),
  );
  return useMemo(() => {
    const inTarget = (data?.resources ?? []).filter(
      (candidate) => candidate.target_key === record.target_key,
    );
    return {
      loaded: data !== null,
      error,
      find: (kind, identifier, groupScope) =>
        inTarget.find(
          (candidate) =>
            candidate.kind === kind &&
            candidate.identifier === identifier &&
            (groupScope === undefined || scalar(candidate.facts, "group_scope") === groupScope),
        ),
      snapshotsOf: (scope, identifier) =>
        inTarget
          .filter(
            (candidate) =>
              candidate.kind === "snapshot" &&
              scalar(candidate.facts, "snapshot_scope") === scope &&
              scalar(candidate.relationships, "source_identifier") === identifier,
          )
          .sort((first, second) =>
            String(scalar(second.facts, "created_at_source_utc") ?? "").localeCompare(
              String(scalar(first.facts, "created_at_source_utc") ?? ""),
            ),
          ),
    };
  }, [data, error, record.target_key]);
}
