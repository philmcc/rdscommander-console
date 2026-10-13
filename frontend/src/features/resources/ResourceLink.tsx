import { Link } from "react-router";
import type { ResourceRecord } from "@/api/types";
import { resourceIdFromArn } from "@/lib/resourceId";

interface ResourceLinkProps {
  /** The resource the name refers to, when the inventory holds it. */
  target: ResourceRecord | undefined;
  /** The name as AWS gives it; shown whether or not it can be linked. */
  identifier: string;
}

/**
 * A resource's identifier that opens the resource's page; plain monospace text when the resource is not in
 * the inventory (yet), because a link to nowhere helps no one.
 *
 * @example
 * <ResourceLink target={directory.find("cluster", "demo-aurora")} identifier="demo-aurora" />
 */
export function ResourceLink({ target, identifier }: ResourceLinkProps) {
  if (!target) return <span className="font-mono">{identifier}</span>;
  return (
    <Link
      to={`/resources/${resourceIdFromArn(target.arn)}`}
      className="font-mono text-accent hover:underline"
    >
      {identifier}
    </Link>
  );
}

interface ResourceLinksProps {
  /** Names to show, in order. */
  identifiers: string[];
  /** Resolves a name to the resource it refers to. */
  find: (identifier: string) => ResourceRecord | undefined;
}

/**
 * A comma-separated run of `ResourceLink`s; a dash when there are none.
 *
 * @example
 * <ResourceLinks identifiers={["demo-orders-replica"]} find={(id) => directory.find("instance", id)} />
 */
export function ResourceLinks({ identifiers, find }: ResourceLinksProps) {
  if (identifiers.length === 0) return <span className="font-mono">—</span>;
  return (
    <span className="font-mono">
      {identifiers.map((identifier, index) => (
        <span key={identifier}>
          {index > 0 && ", "}
          <ResourceLink target={find(identifier)} identifier={identifier} />
        </span>
      ))}
    </span>
  );
}
