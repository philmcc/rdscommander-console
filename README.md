# RDSCommander console

A local web console that manages a PostgreSQL estate on AWS (RDS for PostgreSQL and Aurora PostgreSQL). It
discovers what exists, changes it through reviewed and approved operations, keeps Terraform for the
resources it manages, and records everything it ever ran.

This repository is the console itself. A company uses it by forking it, or by keeping a copy of it under
its own GitHub account, and adding its own configuration next to `app/`.

## Layout

| Path | Owner | Notes |
|---|---|---|
| `app/` | This repository | The console. Change it here, then merge it into each company's copy. |
| `config/` | The company | Environments, company identity, Terraform settings. Start from `app/config.example/`. |
| `terraform/` | The console | Written by the console for each target. |
| `docs/` | The company | Runbooks. |
| `.env` | The operator | Per-operator settings. Git-ignored. |

Upstream never contains `config/`, `terraform/`, `docs/` or `.env`, so merging new console versions into a
company's copy does not conflict with its own files.

## Set up a company copy

1. Fork this repository, or create an empty repository under the company's GitHub account and push this one
   to it.
2. Copy the example configuration: `cp -R app/config.example config`, then edit `config/company.yaml` and
   `config/environments.yaml`.
3. Start the console from the repository root with `app/bin/console up`, then open http://127.0.0.1:8470.
   The first run writes `.env` from your git identity.

Details: `app/docs/getting-started.md` and `app/docs/operating-model.md`.

## Keep versions in step with git

Add this repository as a second remote in each company's copy, then merge when you want a new version:

```
git remote add upstream <url of this repository>
git fetch upstream
git merge upstream/main
```

Releases are tags named `v<version>`, with notes in `app/CHANGELOG.md`. Make changes to the console in this
repository first and merge them down. A change made only inside one company's copy will conflict with the
next merge.

## Develop the console

- Run it against the simulated AWS account: `eval "$(app/bin/console dev-init)"`, then `app/bin/console up`.
- Backend checks: `cd app/backend && uv sync && uv run ruff check . && uv run pytest -q`.
- Frontend checks: `cd app/frontend && bun install --frozen-lockfile && bun run lint && bun run typecheck && bun run test`.
