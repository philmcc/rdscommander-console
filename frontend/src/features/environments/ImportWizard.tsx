import type { ProfileList } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useTask } from "@/features/tasks/TaskContext";
import { useApiGet } from "@/features/tasks/widgets/useApiGet";

/**
 * Pre-step of an environment import: pick an AWS profile from this machine, or the simulated account. The
 * choice opens the import task with the matching options filled in; the task panel collects the rest (name,
 * regions, mode) and runs the import.
 *
 * @example
 * <ImportWizard />
 */
export function ImportWizard() {
  const { open } = useTask();
  const profiles = useApiGet<ProfileList>("/api/v1/environments/import/profiles");
  const startWithProfile = (profile: string) =>
    open({
      kind: "environment_import",
      initialOptions: {
        aws_profile_name: profile,
        simulated: false,
        change_management: "terraform",
        regions: [],
      },
    });
  const startWithSimulatedAccount = () =>
    open({
      kind: "environment_import",
      initialOptions: {
        simulated: true,
        aws_profile_name: null,
        regions: ["eu-west-2"],
        change_management: "terraform",
      },
    });
  return (
    <section
      aria-labelledby="import-wizard-heading"
      className="rounded-lg border border-hairline bg-surface p-4"
    >
      <h2 id="import-wizard-heading" className="text-sm font-medium">
        Import an environment
      </h2>
      <p className="mb-3 text-sm text-muted">
        Choose the AWS profile from this machine's <code className="font-mono">~/.aws/config</code>. The
        console confirms the account, scans regions for PostgreSQL, writes the configuration and commits it.
      </p>
      {profiles.error && (
        <p role="alert" className="mb-3 text-sm text-status-red">
          Could not read the AWS profiles: {profiles.error}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {profiles.data === null && !profiles.error && (
          <span className="text-sm text-muted">Reading profiles…</span>
        )}
        {profiles.data?.profiles.map((profile) => (
          <Button
            key={profile}
            variant="outline"
            onClick={() => startWithProfile(profile)}
            className="font-mono"
          >
            {profile}
          </Button>
        ))}
        {profiles.data?.profiles.length === 0 && (
          <span className="text-sm text-muted">
            No profiles found; add one with <code className="font-mono">aws configure sso</code>.
          </span>
        )}
        <Button variant="ghost" onClick={startWithSimulatedAccount}>
          Use the simulated account
        </Button>
      </div>
    </section>
  );
}
