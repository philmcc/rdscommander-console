import { isValidElement } from "react";
import { displayValue } from "@/lib/recordValues";

export interface Fact {
  label: string;
  /** Text, a number, a flag (shown as yes or no), nothing (shown as a dash) or an element such as a link. */
  value: unknown;
  /** Identifiers, versions and values are monospace; pass `false` for prose. */
  mono?: boolean;
}

function showFact(value: unknown) {
  if (isValidElement(value)) return value;
  if (typeof value === "boolean") return value ? "yes" : "no";
  return displayValue(value);
}

/**
 * Label and value pairs in a dense grid.
 *
 * @example
 * <FactsGrid facts={[{ label: "Class", value: "db.m6g.large" }, { label: "Multi-AZ", value: true }]} />
 */
export function FactsGrid({ facts }: { facts: Fact[] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {facts.map((fact) => (
        <div key={fact.label} className="contents">
          <dt className="text-muted">{fact.label}</dt>
          <dd className={fact.mono === false ? "min-w-0" : "min-w-0 break-words font-mono"}>
            {showFact(fact.value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
