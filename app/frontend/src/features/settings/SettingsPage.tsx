import { type ReactNode, useId, useSyncExternalStore } from "react";
import { Link } from "react-router";
import type { EnvironmentOverview, EnvironmentSummary } from "@/api/types";
import { environmentHue, useEnvironment } from "@/app/EnvironmentContext";
import { EnvironmentChip } from "@/components/EnvironmentChip";
import { PageHeader } from "@/components/PageHeader";
import { SimulatedBadge } from "@/components/SimulatedBadge";
import { StatusBadge } from "@/components/StatusBadge";
import { useEnvironmentOverviews } from "@/features/environments/useEnvironmentOverviews";
import { FactsGrid } from "@/features/resources/FactsGrid";
import { ResourceSection } from "@/features/resources/ResourceSection";
import { cn } from "@/lib/utils";
import { applyTheme, getThemeSnapshot, subscribeTheme, type ThemePreference } from "@/theme";

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/** A file or setting name inside a sentence. */
function Code({ children }: { children: ReactNode }) {
  return <code className="font-mono text-xs">{children}</code>;
}

/**
 * The theme preference as three radios; the choice is applied at once and remembered in this browser. The
 * radios read the preference from the theme store, so they follow the top-bar toggle too.
 */
function ThemeChoice() {
  const { preference } = useSyncExternalStore(subscribeTheme, getThemeSnapshot);
  const groupName = useId();
  return (
    <>
      <div role="radiogroup" aria-label="Theme" className="flex flex-wrap gap-5">
        {THEMES.map(({ value, label }) => (
          <label key={value} className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={groupName}
              value={value}
              checked={preference === value}
              onChange={() => applyTheme(value)}
            />
            {label}
          </label>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted">
        Remembered in this browser only. System follows your operating system.
      </p>
    </>
  );
}

interface EnvironmentSettingsProps {
  environment: EnvironmentSummary;
  /** The environment's refresh and drift intervals; undefined while they load or when they could not be read. */
  overview: EnvironmentOverview | undefined;
}

/** One environment as loaded: its chip and mode, the flags and intervals that govern it, and its targets. */
function EnvironmentSettings({ environment, overview }: EnvironmentSettingsProps) {
  const { environments } = useEnvironment();
  return (
    <article
      aria-label={environment.display_name}
      className={cn(
        "rounded-lg border border-hairline bg-paper p-4",
        environment.is_production && "border-l-4 border-l-status-red",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <EnvironmentChip environment={environment} hue={environmentHue(environments, environment.slug)} />
        <StatusBadge status={environment.change_management} />
      </div>
      <p className="mt-1 font-mono text-xs text-muted">{environment.slug}</p>
      <div className="mt-3">
        <FactsGrid
          facts={[
            {
              label: "Foreign-owned changes allowed",
              value: environment.allow_direct_changes_to_foreign_owned,
            },
            { label: "Approval required", value: environment.requires_approval },
            {
              label: "Inventory refresh every",
              value: overview ? `${overview.inventory_refresh_minutes} minutes` : null,
            },
            { label: "Drift check every", value: overview ? `${overview.drift_check_hours} hours` : null },
          ]}
        />
      </div>
      <ul aria-label={`Targets of ${environment.display_name}`} className="mt-3 space-y-1 text-sm">
        {environment.targets.map((target) => (
          <li key={target.target_key} className="flex flex-wrap items-center gap-x-2">
            <span className="font-mono">{target.aws_account_id}</span>
            <span className="font-mono text-muted">{target.aws_region}</span>
            {target.aws_profile_name && <span className="font-mono">{target.aws_profile_name}</span>}
            <SimulatedBadge simulated={target.simulated} />
          </li>
        ))}
      </ul>
    </article>
  );
}

/**
 * The configuration as the console loaded it: operator and version, the theme preference, each environment
 * with its mode, flags, intervals and targets, where the Terraform settings live, and the company. Each
 * section says where the setting is changed. The theme is the only thing changed here.
 *
 * @example
 * <Route path="settings" element={<SettingsPage />} />
 */
export default function SettingsPage() {
  const { bootstrap, environments } = useEnvironment();
  const { environments: overviews, error: overviewsError } = useEnvironmentOverviews();
  return (
    <>
      <PageHeader breadcrumb={[{ label: "Settings" }]} title="Settings" />
      {!bootstrap ? (
        <p className="text-muted">Loading…</p>
      ) : (
        <div className="space-y-4">
          <ResourceSection title="Console">
            <FactsGrid
              facts={[
                { label: "Operator", value: bootstrap.operator_name },
                { label: "Console version", value: bootstrap.engine_version },
              ]}
            />
            <p className="mt-3 text-xs text-muted">
              The operator name comes from <Code>RDSC_OPERATOR_NAME</Code> in the <Code>.env</Code> file the
              console starts with.
            </p>
          </ResourceSection>
          <ResourceSection title="Theme">
            <ThemeChoice />
          </ResourceSection>
          <ResourceSection title="Environments">
            {overviewsError && (
              <p role="alert" className="mb-3 text-sm text-status-red">
                Could not read the refresh and drift intervals: {overviewsError}
              </p>
            )}
            {environments.length === 0 ? (
              <p className="text-sm text-muted">
                No environment is configured.{" "}
                <Link to="/environments" className="text-accent hover:underline">
                  Import one
                </Link>{" "}
                to begin.
              </p>
            ) : (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {environments.map((environment) => (
                  <EnvironmentSettings
                    key={environment.slug}
                    environment={environment}
                    overview={overviews?.find((candidate) => candidate.slug === environment.slug)}
                  />
                ))}
              </div>
            )}
            <p className="mt-3 text-xs text-muted">
              Change these in <Code>config/environments.yaml</Code> and restart, or{" "}
              <Link to="/environments" className="text-accent hover:underline">
                import a new environment
              </Link>
              .
            </p>
          </ResourceSection>
          <ResourceSection title="Terraform">
            <p className="text-sm">
              The console does not show the Terraform settings: the OpenTofu and AWS provider versions and the
              state bucket name template.
            </p>
            <p className="mt-2 text-xs text-muted">
              They live in <Code>config/terraform.yaml</Code>; change them there and restart.
            </p>
          </ResourceSection>
          <ResourceSection title="Company">
            <FactsGrid
              facts={[
                { label: "Name", value: bootstrap.company_display_name, mono: false },
                { label: "Slug", value: bootstrap.company_slug },
              ]}
            />
            <p className="mt-3 text-xs text-muted">
              Change these in <Code>config/company.yaml</Code> and restart.
            </p>
          </ResourceSection>
        </div>
      )}
    </>
  );
}
