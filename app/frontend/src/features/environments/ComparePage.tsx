import { type FormEvent, useId, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router";
import type { EnvironmentComparison, EnvironmentSummary } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { DiffTable } from "@/components/DiffTable";
import { PageHeader } from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ResourceSection } from "@/features/resources/ResourceSection";
import { cn } from "@/lib/utils";
import { useEnvironmentComparison } from "./useEnvironmentComparison";

/** What the operator has typed or chosen since the comparison in the address was asked for. */
interface Draft {
  left?: string;
  right?: string;
  strip?: string;
}

/** The environment after `slug` in configuration order, wrapping round; empty when there is no other one. */
function nextSlug(environments: EnvironmentSummary[], slug: string): string {
  const index = environments.findIndex((environment) => environment.slug === slug);
  const next = environments[(index + 1) % Math.max(environments.length, 1)];
  return next && next.slug !== slug ? next.slug : "";
}

interface EnvironmentSelectProps {
  label: string;
  value: string;
  environments: EnvironmentSummary[];
  onChange: (slug: string) => void;
}

/** A native select over the configured environments; a slug from the address that is not one still shows. */
function EnvironmentSelect({ label, value, environments, onChange }: EnvironmentSelectProps) {
  const id = useId();
  const known = environments.some((environment) => environment.slug === value);
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={id}>{label}</label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 w-full rounded-md border border-hairline bg-surface px-2"
      >
        {value === "" && <option value="">Choose an environment</option>}
        {value !== "" && !known && <option value={value}>{value}</option>}
        {environments.map((environment) => (
          <option key={environment.slug} value={environment.slug}>
            {environment.display_name}
          </option>
        ))}
      </select>
    </div>
  );
}

interface PairSideProps {
  /** The environment this side of the pair is in. */
  environmentSlug: string;
  identifier: string;
  resourceId: string;
  /** The environment caption above the identifier (a column header) rather than before it (a row). */
  stacked?: boolean;
}

/**
 * One side of a pair: the environment as a small caption (a production environment is marked as such) and
 * the identifier as a link to its resource page. The link says which environment it is in, because the two
 * sides of a pair often have the same identifier.
 */
function PairSide({ environmentSlug, identifier, resourceId, stacked = false }: PairSideProps) {
  const { environments } = useEnvironment();
  const environment = environments.find((candidate) => candidate.slug === environmentSlug);
  const environmentName = environment?.display_name ?? environmentSlug;
  return (
    <span
      className={cn("inline-flex gap-x-2", stacked ? "flex-col items-start gap-y-0.5" : "items-baseline")}
    >
      <span aria-hidden="true" className="inline-flex items-center gap-1.5 text-xs font-normal text-muted">
        <span
          className="inline-block size-1.5 rounded-full"
          style={{ background: `var(--env-hue-${environmentHue(environments, environmentSlug)})` }}
        />
        {environmentName}
        {environment?.is_production && (
          <span className="uppercase tracking-wide text-status-red">production</span>
        )}
      </span>
      <Link
        to={`/resources/${resourceId}`}
        aria-label={`${identifier} in ${environmentName}`}
        className="font-mono text-accent hover:underline"
      >
        {identifier}
      </Link>
    </span>
  );
}

/** The matched pairs, then what has no counterpart on either side. */
function ComparisonResult({ comparison }: { comparison: EnvironmentComparison }) {
  const { environments } = useEnvironment();
  const nameOf = (slug: string) =>
    environments.find((environment) => environment.slug === slug)?.display_name ?? slug;
  const differing = comparison.pairs.filter((pair) => (pair.differences ?? []).length > 0);
  const alike = comparison.pairs.filter((pair) => (pair.differences ?? []).length === 0);
  const sides = [
    { slug: comparison.left_slug, unmatched: comparison.unmatched_left },
    { slug: comparison.right_slug, unmatched: comparison.unmatched_right },
  ];
  return (
    <div className="mt-6 space-y-4">
      <p className="text-sm text-muted">
        {comparison.pairs.length} matched {comparison.pairs.length === 1 ? "pair" : "pairs"},{" "}
        {differing.length} with differences.
      </p>
      {differing.map((pair) => (
        <section key={`${pair.left_resource_id}-${pair.right_resource_id}`} className="space-y-1">
          <h3 className="text-xs uppercase tracking-wide text-muted">{pair.kind.replaceAll("_", " ")}</h3>
          <div className="rounded-lg border border-hairline bg-surface">
            <DiffTable
              rows={(pair.differences ?? []).map((difference) => ({
                attribute: difference.attribute,
                before: difference.left,
                after: difference.right,
              }))}
              beforeLabel={
                <PairSide
                  stacked
                  environmentSlug={comparison.left_slug}
                  identifier={pair.left_identifier}
                  resourceId={pair.left_resource_id}
                />
              }
              afterLabel={
                <PairSide
                  stacked
                  environmentSlug={comparison.right_slug}
                  identifier={pair.right_identifier}
                  resourceId={pair.right_resource_id}
                />
              }
            />
          </div>
        </section>
      ))}
      {alike.length > 0 && (
        <ul aria-label="Pairs without differences" className="rounded-lg border border-hairline bg-surface">
          {alike.map((pair) => (
            <li
              key={`${pair.left_resource_id}-${pair.right_resource_id}`}
              className="flex min-h-9 flex-wrap items-center gap-x-4 gap-y-1 border-b border-hairline px-4 py-1 text-sm last:border-0"
            >
              <span className="w-28 shrink-0 text-xs uppercase tracking-wide text-muted">
                {pair.kind.replaceAll("_", " ")}
              </span>
              <PairSide
                environmentSlug={comparison.left_slug}
                identifier={pair.left_identifier}
                resourceId={pair.left_resource_id}
              />
              <PairSide
                environmentSlug={comparison.right_slug}
                identifier={pair.right_identifier}
                resourceId={pair.right_resource_id}
              />
              <span className="ml-auto text-muted">no differences</span>
            </li>
          ))}
        </ul>
      )}
      <div className="grid grid-cols-1 items-start gap-4 md:grid-cols-2">
        {sides.map(({ slug, unmatched }) => (
          <ResourceSection key={slug} title={`Only in ${nameOf(slug)}`}>
            <ul className="space-y-1 text-sm">
              {unmatched.map((resource) => (
                <li key={resource.resource_id} className="flex items-center gap-2">
                  <span className="w-28 shrink-0 text-xs uppercase tracking-wide text-muted">
                    {resource.kind.replaceAll("_", " ")}
                  </span>
                  <Link
                    to={`/resources/${resource.resource_id}`}
                    className="font-mono text-accent hover:underline"
                  >
                    {resource.identifier}
                  </Link>
                </li>
              ))}
              {unmatched.length === 0 && <li className="text-muted">Nothing without a counterpart.</li>}
            </ul>
          </ResourceSection>
        ))}
      </div>
    </div>
  );
}

/**
 * Two environments side by side: resources matched by identifier (after stripping the listed suffixes or
 * prefixes) and parameter groups matched by name, with a table of what differs in each pair and the
 * resources that have no counterpart. The environments and strip list live in the address
 * (`?left=&right=&strip=`), and an address that names both environments compares at once.
 *
 * @example
 * <Route path="environments/compare" element={<ComparePage />} />
 */
export default function ComparePage() {
  const { environments, selected } = useEnvironment();
  const [searchParams, setSearchParams] = useSearchParams();
  const [draft, setDraft] = useState<Draft>({});
  const { key: askedAt } = useLocation();
  const stripId = useId();
  const stripHelpId = useId();

  const addressLeft = searchParams.get("left") ?? "";
  const addressRight = searchParams.get("right") ?? "";
  const addressStrip = searchParams.get("strip") ?? "";

  const left = draft.left ?? (addressLeft || selected?.slug || "");
  const right = draft.right ?? (addressRight || nextSlug(environments, left));
  const strip = draft.strip ?? addressStrip;
  const canCompare = left !== "" && right !== "" && left !== right;

  const { comparison, error, busy } = useEnvironmentComparison(
    addressLeft && addressRight
      ? { left: addressLeft, right: addressRight, strip: addressStrip, askedAt }
      : null,
  );

  const compare = (event: FormEvent) => {
    event.preventDefault();
    if (!canCompare) return;
    const next = new URLSearchParams({ left, right });
    if (strip.trim()) next.set("strip", strip);
    setSearchParams(next);
    setDraft({});
  };

  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "Environments", to: "/environments" }, { label: "Compare" }]}
        title="Compare environments"
      />
      <form onSubmit={compare} className="grid items-end gap-4 md:grid-cols-[1fr_1fr_1.5fr_auto]">
        <EnvironmentSelect
          label="Left environment"
          value={left}
          environments={environments}
          onChange={(slug) => setDraft((current) => ({ ...current, left: slug }))}
        />
        <EnvironmentSelect
          label="Right environment"
          value={right}
          environments={environments}
          onChange={(slug) => setDraft((current) => ({ ...current, right: slug }))}
        />
        <div className="space-y-1 text-sm">
          <label htmlFor={stripId}>Strip suffixes or prefixes</label>
          <Input
            id={stripId}
            value={strip}
            placeholder="-prod, -prd"
            aria-describedby={stripHelpId}
            onChange={(event) => setDraft((current) => ({ ...current, strip: event.target.value }))}
          />
        </div>
        <Button type="submit" disabled={!canCompare || busy}>
          Compare
        </Button>
      </form>
      <p id={stripHelpId} className="mt-1 text-xs text-muted">
        Comma-separated. Resources whose identifiers match once these are stripped are paired; parameter
        groups are paired by name.
      </p>
      {left !== "" && left === right && (
        <p className="mt-2 text-sm text-status-amber">Choose two different environments.</p>
      )}
      {error && (
        <p role="alert" className="mt-4 text-sm text-status-red">
          Could not compare: {error}
        </p>
      )}
      {busy && !comparison && <p className="mt-4 text-muted">Comparing…</p>}
      {comparison && <ComparisonResult comparison={comparison} />}
    </>
  );
}
