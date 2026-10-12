# Getting started

## Install

1. Docker Desktop (or any Docker engine with Compose v2).
2. An AWS CLI profile for every real target in `config/environments.yaml`.
   `aws configure sso` works well; the console mounts `~/.aws` read-only and
   uses the profile names from the configuration. The simulated sandbox needs
   no credentials.

## Git access from inside the console

The console runs inside a container and must reach this repository's remote
to check that your checkout is current before it plans. `app/bin/console up`
mounts, read-only, your `~/.ssh` (keys and known hosts), `~/.gitconfig`,
`~/.git-credentials`, and your SSH agent socket. Either works:

- SSH remotes: make sure `ssh -T git@github.com` succeeds on your machine with
  the agent running; the container uses the same agent.
- HTTPS remotes: store a token once with `git config --global credential.helper store`
  followed by one `git fetch` that prompts for it; the token lands in
  `~/.git-credentials`, which the container reads.

## Start

From the repository root:

```bash
app/bin/console up
```

The first run writes `.env` next to the repository root from your git identity.
Open http://127.0.0.1:8470 when the script prints `console_ready`.

`app/bin/console down` stops everything; `app/bin/console logs` follows the backend.

## Per-operator settings (`.env`)

| Key | Meaning |
|---|---|
| `RDSC_OPERATOR_NAME` | Recorded on every operation and approval |
| `RDSC_GIT_AUTHOR_NAME`, `RDSC_GIT_AUTHOR_EMAIL` | Identity for commits the console makes |
| `RDSC_CONTROL_DATABASE_URL` | Optional. When set, no bundled Postgres starts and several people can share one console state |
| `RDSC_HTTP_PORT` | Default 8470 |
| `RDSC_TERRAFORM_BINARY` | `tofu` (default) or `terraform` |
| `RDSC_INVENTORY_SNAPSHOT_RETENTION_PER_TARGET` | Default 96. Inventory snapshots each target keeps; older ones are pruned after every refresh unless the change history, a drift finding or an operation needs them (see the README's "Control database") |
| `RDSC_EXECUTION_RECORD_RETENTION_DAYS` | Default 30. Execution records of finished inventory refreshes and drift detections, and of no operation, are pruned after this many days |
| `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS` | Default 14400 (four hours). How long a major upgrade waits for AWS to make the green side of its blue/green deployment available; past it the upgrade fails and leaves the deployment in place to be cleaned up once AWS reports it available |
| `RDSC_RUNNER_IDENTITY_NAME` | Optional. Replaces the host name in the console's runner identity (`<name>:<operator>`). The compose file already fixes the container's host name to `rdscommander-console`, so an operation waiting for approval survives `console down` and `up` either way |

A shared control database is any Postgres reachable from your machine. Reaching
it, for example over a VPN or an SSM tunnel, is up to you; the console reports a
clear diagnostic when it cannot connect.

## Configuration (`config/`)

- `company.yaml`: display name and slug (used in resource tags and state bucket names).
- `environments.yaml`: every environment and its targets (one AWS account in one
  region each). Every target is always visible in the console.
- `terraform.yaml`: OpenTofu and AWS provider versions, and the state bucket name template.
- `simulated_estate.yaml` (optional): overrides the demo estate seeded into simulated targets.

## When an adoption fails

Nothing is committed when an adoption fails. The rendered files stay in the
workspace so you can inspect them; the next adoption is blocked until they are
committed or discarded:

```bash
git checkout -- terraform && git clean -fd terraform
```

## Upgrading

Merge the pull request titled "Upgrade console to ..." when it arrives. Nothing
under `config/`, `terraform/`, or `docs/` is touched by an upgrade.
