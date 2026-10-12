import { Link } from "react-router";
import { Button } from "@/components/ui/button";

/** An open drift finding of the environment page, read from the loosely typed `drift` list. */
interface EnvironmentDriftFinding {
  /** The resource's ARN, or the Terraform address of a resource that has left the inventory. */
  key: string;
  /** The page of the resource; `null` for a finding keyed by a Terraform address, which has no page. */
  resourceId: string | null;
  findingKind: string;
  detectedAtUtc: string;
  attributes: { attribute: string; recorded: unknown; live: unknown }[];
}

/** The attribute name a finding uses to say that a resource exists on one side only. */
const PRESENCE_ATTRIBUTE = "presence";

function readFinding(raw: Record<string, unknown>): EnvironmentDriftFinding | null {
  const { resource_arn, resource_id, finding_kind, detected_at_utc, attributes } = raw;
  if (
    typeof resource_arn !== "string" ||
    typeof finding_kind !== "string" ||
    typeof detected_at_utc !== "string"
  ) {
    return null;
  }
  const readAttributes = Array.isArray(attributes) ? attributes : [];
  return {
    key: resource_arn,
    resourceId: typeof resource_id === "string" ? resource_id : null,
    findingKind: finding_kind,
    detectedAtUtc: detected_at_utc,
    attributes: readAttributes.flatMap((entry: unknown) => {
      if (typeof entry !== "object" || entry === null) return [];
      const { attribute, recorded, live } = entry as Record<string, unknown>;
      return typeof attribute === "string" ? [{ attribute, recorded, live }] : [];
    }),
  };
}

/** Says which side holds a resource that the other lacks. */
function presenceSentence(finding: EnvironmentDriftFinding): string | null {
  const presence = finding.attributes.find((entry) => entry.attribute === PRESENCE_ATTRIBUTE);
  if (!presence) return null;
  return presence.recorded === true && presence.live === false
    ? "In the record, missing from AWS"
    : "In AWS, missing from the record";
}

function identifierOf(key: string): string {
  return key.startsWith("arn:") ? (key.split(":").at(-1) ?? key) : key;
}

interface EnvironmentDriftProps {
  /** `drift` of the environment page. */
  findings: Record<string, unknown>[];
  /** Called with the finding's key when the operator accepts a finding that has no resource page. */
  onAccept: (key: string) => void;
}

/**
 * Open drift of an environment. A finding with a resource page links to it, where the differences can be
 * accepted or restored; one that has left the inventory shows its Terraform address and offers to accept it
 * into the record. Says so in green when nothing differs.
 *
 * @example
 * <EnvironmentDrift findings={page.drift} onAccept={(key) => openAcceptTask(key)} />
 */
export function EnvironmentDrift({ findings, onAccept }: EnvironmentDriftProps) {
  const readable = findings.flatMap((raw) => {
    const finding = readFinding(raw);
    return finding ? [finding] : [];
  });
  if (readable.length === 0) return <p className="text-sm text-status-green">Matches the record.</p>;
  return (
    <ul className="space-y-2">
      {readable.map((finding) => {
        const presence = presenceSentence(finding);
        const names = finding.attributes
          .filter((entry) => entry.attribute !== PRESENCE_ATTRIBUTE)
          .map((entry) => entry.attribute);
        return (
          <li
            key={`${finding.key}-${finding.findingKind}`}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-hairline pb-2 text-sm last:border-0"
          >
            {finding.resourceId ? (
              <Link to={`/resources/${finding.resourceId}`} className="font-mono text-accent hover:underline">
                {identifierOf(finding.key)}
              </Link>
            ) : (
              <span className="font-mono">{finding.key}</span>
            )}
            {names.length > 0 && <span className="font-mono text-muted">{names.join(", ")}</span>}
            {presence && <span className="text-status-amber">{presence}</span>}
            <span className="text-xs text-muted">
              {finding.findingKind === "record"
                ? "AWS differs from the committed record"
                : "Terraform state differs from the files"}{" "}
              · {new Date(finding.detectedAtUtc).toLocaleString()}
            </span>
            {!finding.resourceId && (
              <Button
                size="sm"
                variant="outline"
                aria-label={`Accept into the record: ${finding.key}`}
                onClick={() => onAccept(finding.key)}
              >
                Accept into the record
              </Button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
