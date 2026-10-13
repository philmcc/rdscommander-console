# Getting started

## Requirements

1. Docker Desktop (or any Docker engine with Compose v2) and git.
2. For real AWS accounts, an AWS CLI profile for every target in `config/environments.yaml`.
   `aws configure sso` works well; the console mounts `~/.aws` read-only and uses the profile names from
   the configuration. The simulated sandbox needs no credentials.

## Start

From the repository root:

```bash
bin/console up
```

The first start writes `.env` from your git identity and creates the console's home. Open
http://127.0.0.1:8470 when the script prints `console_ready`. `bin/console down` stops everything and
`bin/console logs` follows the backend.

## The home

The console keeps its configuration (`config/`) and the Terraform it maintains (`terraform/`) in a git
repository called its home. By default that is `home/` inside this checkout, created on first start with the
example configuration and ignored by this repository's git. The console commits there and never pushes.

To keep them in a repository of your own, for example one you push to a remote for review, set
`RDSC_HOME_DIR` in `.env` to its path. If it has no `config/` yet, the example configuration is copied in
for you to review and commit. Add these lines to its `.gitignore` so Terraform's working files stay out:

```
terraform/**/.terraform/
terraform/**/*.tfplan
```

## Git access from inside the console

The console runs inside a container. Before it plans, it checks that the home repository is current, which
needs to reach that repository's remote if it has one. `bin/console up` mounts, read-only, your `~/.ssh`
(keys and known hosts), `~/.gitconfig`, `~/.git-credentials` and your SSH agent socket. Either works:

- SSH remotes: make sure `ssh -T git@github.com` succeeds on your machine with the agent running; the
  container uses the same agent.
- HTTPS remotes: store a token once with `git config --global credential.helper store` followed by one
  `git fetch` that prompts for it; the token lands in `~/.git-credentials`, which the container reads.

## Per-operator settings (`.env`)

| Key | Meaning |
|---|---|
| `RDSC_OPERATOR_NAME` | Recorded on every operation and approval |
| `RDSC_GIT_AUTHOR_NAME`, `RDSC_GIT_AUTHOR_EMAIL` | Identity for commits the console makes |
| `RDSC_HOME_DIR` | Optional. The git repository holding `config/` and `terraform/`. Default `home/` here |
| `RDSC_CONTROL_DATABASE_URL` | Optional. When set, no bundled Postgres starts and several people can share one console state |
| `RDSC_BUNDLED_DATABASE_PASSWORD` | Written on first start. The password of the bundled Postgres |
| `RDSC_HTTP_PORT` | Default 8470 |
| `RDSC_TERRAFORM_BINARY` | `tofu` (default) or `terraform` |
| `RDSC_INVENTORY_SNAPSHOT_RETENTION_PER_TARGET` | Default 96. Inventory snapshots each target keeps; older ones are pruned after every refresh unless the change history, a drift finding or an operation needs them (see the README's "Control database") |
| `RDSC_EXECUTION_RECORD_RETENTION_DAYS` | Default 30. Execution records of finished inventory refreshes and drift detections, and of no operation, are pruned after this many days |
| `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS` | Default 14400 (four hours). How long a major upgrade waits for AWS to make the green side of its blue/green deployment available; past it the upgrade fails and leaves the deployment in place to be cleaned up once AWS reports it available |
| `RDSC_RUNNER_IDENTITY_NAME` | Optional. Replaces the host name in the console's runner identity (`<name>:<operator>`). The compose file already fixes the container's host name to `rdscommander-console`, so an operation waiting for approval survives `console down` and `up` either way |

A shared control database is any Postgres reachable from your machine. Reaching it, for example over a VPN
or an SSM tunnel, is up to you; the console reports a clear diagnostic when it cannot connect.

## Configuration (`config/` in the home)

- `environments.yaml`: every environment and its targets (one AWS account in one region each). Every target
  is always visible in the console.
- `terraform.yaml`: OpenTofu and AWS provider versions, and the state bucket name template, which may use
  `{aws_account_id}` and `{aws_region}` (default `rdscommander-state-{aws_account_id}-{aws_region}`).
- `simulated_estate.yaml` (optional): overrides the demo estate seeded into simulated targets.

## When an adoption fails

Nothing is committed when an adoption fails. The rendered files stay in the home repository so you can
inspect them; the next adoption is blocked until they are committed or discarded:

```bash
cd home && git checkout -- terraform && git clean -fd terraform
```

## Updating

`git pull`, then `bin/console up`. The home, `.env` and the control database are not touched by an update,
and database migrations run when the console starts. Read `CHANGELOG.md` for anything to do by hand.
