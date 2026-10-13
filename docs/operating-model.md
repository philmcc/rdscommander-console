# Operating model

## Environments and targets

An environment (production, staging, sandbox) is a named group of targets. A
target is one AWS account in one region. Inventory snapshots, operations,
Terraform workspaces and drift findings are all scoped to a target. All
environments are always visible; the switcher only changes which one is in
focus.

## Change-management modes

Each environment sets `change_management` in `config/environments.yaml`.

- **Terraform mode** (the default): a change renders the Terraform files,
  plans, pauses for approval, applies the approved plan, checks that a second
  plan is clean, commits, and refreshes the target's inventory. Resources must
  be adopted (brought under Terraform) before they can be changed.
- **Direct mode**: a change reads the resource from AWS, shows the change set
  (attribute, current value, desired value, how it applies, reboot, downtime),
  pauses for approval, calls the AWS API, reads the values back until AWS
  reports them, refreshes the target's inventory, and then writes the
  Terraform files and the record of what changed and commits them. The refresh
  comes before the record because the record is written from it.

In both modes every change is one commit,
`<task>: <resource> (<operator>, approved by <approver>)`, and the console never
pushes. Resources owned by other tooling (CloudFormation, another Terraform
state) are read-only unless the environment sets
`allow_direct_changes_to_foreign_owned`.

The README's "Switching modes" explains what happens when an environment
changes mode.

## Operations

Everything the console does is an operation: persisted first, then executed by
a background runner. Each operation is a fixed list of steps. Before a step
does anything it observes real state; when the goal is already true the step
is skipped. That is what makes every operation safe to resume after a crash, a
restart, or a takeover by another console sharing the same control database.

States: `QUEUED`, `RUNNING`, `AWAITING_APPROVAL`, `SUCCEEDED`, `FAILED`,
`CANCELLED`.

Failures produce diagnostics with a fix suggestion. Diagnostics are never
deleted.

The runner leases an operation under its identity, `<host name>:<operator>`.
The compose file fixes the container's host name (`rdscommander-console`), and
`RDSC_RUNNER_IDENTITY_NAME` can replace it, so the identity survives
`console down` and `up`.

### Kinds of operation

Besides the parameter group, adoption and drift operations, the console runs these
families, all listed in the README's "Instances, clusters and snapshots":

- **Attribute changes** (instance class, storage, Multi-AZ, backup settings,
  maintenance window, monitoring, deletion protection, serverless capacity, tags):
  Terraform mode renders, plans, applies and commits; direct mode computes the change
  set, calls the API and polls until AWS reports the new values. Direct mode can queue
  instance class, storage, Multi-AZ and backup changes for the next maintenance window;
  monitoring changes (enhanced monitoring and Performance Insights) always apply at once.
  Terraform mode refuses a window request and applies now only.
- **Direct actions** (reboot, fail over, apply pending maintenance, take or delete a
  snapshot, promote a replica): the same AWS calls in both modes, then a
  refresh of the resource, and a reconcile step when the action changes a rendered
  attribute.
- **Reader removal** is a resource change like the creates: Terraform mode renders,
  plans, applies a plain delete and commits; direct mode deletes the instance through
  the API, refreshes and commits the record.
- **Resource creates** (read replica, Aurora reader, restore from a snapshot, point-in-time
  restore, create instance, create cluster): the new resource is rendered into the files.
  A Terraform-mode create ends with a second commit that records the resource as built;
  a direct-mode create sends what the render carries, then a modify for settings the
  create call cannot take. Create-only arguments stay in the record and drift ignores them.
- **Upgrades** (auto minor upgrade, minor upgrade, major upgrade, blue/green cleanup), described
  in the README's "Upgrades". A minor upgrade is an attribute change of the engine version (in
  Terraform mode it also turns auto minor upgrade off when that was on, and leaves it off). A major
  upgrade is its own operation of eleven steps that creates an RDS blue/green deployment, waits for
  its green side, pauses for a second approval, switches over, and records and reconciles the result.
  Its preflight (README, "Preflight") includes logical replication: `rds.logical_replication` must be
  `1` on the resource's parameter group (an Aurora cluster's cluster group) before the upgrade runs.
  Blue/green cleanup is a direct action that deletes a deployment.
- **Decommission** is a two-phase change under one approval: turn deletion protection off,
  then delete with a final snapshot. In Terraform mode it is two applies and two commits;
  the review shows every resource it deletes and open drift on any of them is refused
  before approval.

## Approval

Every operation that changes AWS pauses for an operator's approval, in every
environment and in both modes; `requires_approval` cannot be turned off. That
covers adoptions, every parameter group task, every lifecycle operation (settings
changes, reboots, failovers, snapshots, replicas and readers, restores, creates and
decommissions), every upgrade task and drift restores. The
review shows the Terraform plan (Terraform mode) or the change set (direct
mode), with the expected reboot and downtime. Tasks that destroy something or
interrupt service ask the approver to type the resource's identifier: deletes,
attaches, reboots, failovers, replica promotions, instance class changes, snapshot
deletes, reader removals, decommissions, minor upgrades, blue/green cleanups, the
switchover of a major upgrade, and restores that interrupt service when they are applied (a
restart, a failover, other immediate downtime). Applying pending maintenance
asks for it only when the opt-in is immediate. A restore whose values wait for
the next reboot does not ask for it; its review says the reboot is needed.

The server checks the typed text, not only the page: the approve request carries
`confirmation_text`, and an operation with an `approval_required_text` is approved
only when it is exactly that (otherwise `409`, `confirmation_text_mismatch`). It
also carries `approval_stage`, the stage of the review the approver read; an
operation whose plan names a stage (a major upgrade) refuses an approval for any
other stage (`409`, `approval_stage_mismatch`), so a review page loaded at the first
pause cannot approve the switchover. A refused approval leaves the operation waiting.

### Two-stage approval: major upgrades

A major upgrade is the one operation that pauses twice, because creating a blue/green deployment and
switching production over are different decisions. Nothing changes in AWS or the home repository before the
first approval. The first pause is the review of the green copy: the parameter groups it will use (and whether
it will build them, reuses them or uses existing ones), the preflight checklist, and the statement that
production is not touched until the switchover. Groups the upgrade builds are built after this approval, by an
operation of their own that carries its approver and time. Creating the green side interrupts nothing, so the approver types nothing. After the green side
is available the operation pauses again, with a review of the switchover (the green endpoint to test against,
the rename of the production resource to `<identifier>-old1`, the brief connection drop, and any console
operation that changed production after the green copy was made, which the green side does not carry), and this
approval is confirmed by typing the production identifier (`approval_required_text`). Both go through the same
approve action. The second is recorded separately (`switchover_approved_by`, `switchover_approved_at_utc`
in the plan), so the first approval's operator and time stay as they were. Rejecting at either pause cancels
the operation and, once a deployment exists, leaves it in place (see below).

Right before a Terraform apply the console plans again and compares the plan
with the approved one; any difference aborts the apply and puts the files back.

Operations that change nothing in AWS do not pause: inventory refresh, drift
detection, accepting drift into the record, and environment import.

## Rejecting, and cleaning up after it

Rejecting an operation that waits for approval cancels it and marks it
`cleanup_pending`. A Terraform-mode operation has already rendered its files
into the workspace; the runner of the console that holds those files reverts
the workspace to the last commit and records `cleanup_done_at_utc`. That
console is the one whose runner identity ran the checkout guard or, when the
identity changed, the one whose checkout holds exactly the files the operation
wrote (each writing step records a fingerprint of them). Any other console
leaves the cleanup pending for the right one.

## Blue/green deployments

A major upgrade leaves things in AWS that the console deliberately does not remove on its own.

- **The blue side is never deleted automatically.** After a switchover AWS keeps the old resource as
  `<identifier>-old1` (the next free number if that name is taken; preflight fails instead while `-old1`
  exists). The console tags it `rdscommander:blue-side=<operation id>`, and the inbox lists it with a
  Decommission action. Decommission is an ordinary operation: it asks for the identifier and takes a final
  snapshot. Nothing else removes the old instance, so it keeps costing money until someone decommissions it.
- **A failure at any blue/green step leaves the deployment in place.** Once the deployment exists, an AWS error
  at any later step, a deployment that never becomes available, a deployment AWS marks invalid or a failed
  switchover ends the operation as `FAILED` with a diagnostic that names the deployment and the AWS reason and
  says what is left. Before the switchover production is unchanged. After it, production runs the green side
  and the blue side is kept. Rejecting the first approval creates nothing in AWS or the home repository (the
  green parameter groups are built only after it); rejecting the switchover approval leaves the deployment in
  place with a warning on the operation. Change history keeps crediting the green side, and after a switchover the renames, to the upgrade.
- **Cleanup is its own operation.** "Clean up blue/green deployment" (`blue_green_cleanup`) is the only
  operation that deletes a deployment, and it is offered only while the deployment has not switched over. It
  asks for the production identifier, deletes the green side too unless told to keep it, and never touches
  production. A deployment that has switched over stays in AWS as `SWITCHOVER_COMPLETED`; the console does not
  delete it.
- **Passing AWS failures are ridden out.** The green wait, the switchover poll and the cleanup's settle poll
  ask again, with a backoff of at most a minute, after throttling, an expired login or a lost connection, until
  their own time limit; only then do they fail, naming the deployment left in place.
- **Waiting for the green side** is limited by `blue_green_create_timeout_seconds` (environment variable
  `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS`, 14400 seconds by default), separate from the direct-mode
  verification timeout because copying a large database takes hours. Past it the operation fails and leaves the
  deployment in place to be cleaned up once AWS reports it available.
- **In Terraform mode** the reconcile step after a switchover moves the old standalone instance's state entry to
  `<identifier>_old1` and imports the green instance into the production address, and the plan afterwards must
  be clean; it is committed as the operation's reconcile commit. If the state move fails nothing else has run.
  Every failure after the switchover in Terraform mode names the exact `tofu state mv`, `tofu import` and
  `tofu plan` commands to finish by hand, leaving out the steps an earlier attempt already did; a resumed
  reconcile recognises a move or import that was already done. A verify mismatch after the switchover is a
  warning on the operation, not a failure: the upgrade still reconciles and succeeds.

## Policy guards

- **Adoption**: a plan that would delete or replace an instance or cluster is
  refused; so is a selected resource the plan would create instead of import,
  and a plan that would change a resource adopted earlier (that resource has
  drifted from the record; resolve its drift first).
- **Changes**: only the addresses the operation touches may change, in place.
  A plain create is allowed only for the parameter-group and lifecycle create kinds
  (parameter group create, read replica, Aurora reader, restores, instance and cluster
  creates) and a plain delete only for parameter group delete, removing a reader and
  decommissioning, each only for the addresses the operation creates or deletes; a
  replace never is. A decommission's first phase may delete nothing; only its second
  phase deletes. In direct mode the change set may name
  only the touched addresses.
- **Open drift**: a change to a resource with open record drift is refused,
  when it is requested and again when the change is worked out, with "Resolve
  drift on <resource> first". Otherwise the change would record the outside
  value as part of an approved change nobody reviewed.

## Checkout guard

Before any operation writes a file, the checkout of the home repository is checked:

| Check | Fix |
|---|---|
| Uncommitted changes in the operation's workspace | commit or discard them |
| Files staged outside the workspace | unstage or commit them |
| Branch behind its remote | `git pull` |
| Last applied commit for the workspace missing from the checkout | ask whoever applied it to push, then `git pull` |

A change waiting for approval in one workspace does not block another. When an
operation is resumed after approval and its own rendered files are the only
uncommitted files in the workspace, unchanged since it wrote them, the guard
accepts them.

## Terraform workspaces and what counts as adopted

One workspace per target at `terraform/<environment>/<account>/<region>/`.
State lives in an S3 bucket in that account with native lockfile locking.

A resource is adopted when it is under Terraform: the console adopted it, or
Terraform state holds it (a Terraform-mode change created it, or a console
with another control database adopted it). Detection and adoption read the
workspace's state object to learn the latter. The `rdscommander:managed` tag
is not evidence on its own.

## The record

`console-record.json` sits beside the Terraform files in each workspace. It is
the renderer's input for the committed files, kept so that drift can be found
by comparing structured values instead of parsing HCL. It always describes
committed files: Terraform mode writes it when a change commits, direct mode
when it records the result.

- In Terraform mode it holds the adopted resources. An adoption adds the newly
  adopted resources from the inventory and keeps the earlier ones as recorded.
- In direct mode it holds every resource of the target. The first Detect drift
  records the whole estate in one `Record estate` commit, and every later one
  records the same way each live resource the record lacks. A resource created
  outside the console joins the record at the next detection instead of becoming
  a finding; its creation is in change history as an outside change, recorded
  by the refresh that first saw it.
- A change replaces only the resources it touched, so the drift of every other
  resource stays visible.

## Drift

Detect drift (scheduled every `drift_check_hours`, 24 by default, and in the
launcher) compares the record with the latest inventory snapshot, attribute by
attribute: these are record findings. In Terraform mode it also plans against
the committed files to find state drift; it skips that while the workspace has
uncommitted files (a change waiting for approval, a hand edit). A finding stays
open until detection no longer sees it or an operation resolves it:

- **Accept** writes what AWS has into the record and the files for that one
  resource, and commits. It changes nothing in AWS, so it does not pause.
- **Restore** puts the recorded values back through the environment's change
  strategy, with the usual review and approval.
- A change that commits closes the findings of the resources it touched.

Detection also brings the record in line with the mode, in one guarded commit:
after a switch, and in direct mode at every detection for the live resources
the record lacks (see "The record" and the README's "Switching modes").

## The scheduler

A background scheduler creates the operations the console runs on its own:
an inventory refresh every `inventory_refresh_minutes` (15 by default) and a
drift detection every `drift_check_hours`, per environment, attributed to the
operator `scheduler`. It never creates a second one while one of the same kind
is queued, running or waiting, and it backs off for one cadence after one
failed or was cancelled, so expired credentials do not queue a failing
operation every minute.

## Adoption, step by step

1. check the checkout
2. ensure the state bucket exists with versioning, encryption, and public access blocked
3. render the workspace: earlier adoptions from the record, the selected resources from the latest inventory snapshot
4. write import blocks for the selected resources
5. init
6. plan
7. policy check
8. await approval
9. apply (after re-planning and comparing to the approved plan)
10. verify a second plan shows no changes
11. remove the import blocks and write the record
12. commit to the checkout
13. refresh the inventory (a refresh that fails does not undo the adoption)

Parameter and subnet groups adopt before clusters, and clusters before
instances, because instances reference the others by name.

## The simulated account

A target marked `simulated: true` is served by a local simulator container
instead of AWS. It is seeded with a demo estate at startup and can be reset
from the API. Every guard and approval applies exactly as it does for a real
target, and the console labels it on every screen.

## Records

Every Terraform invocation, AWS API call, and git command is stored with its
full input and output. The History page searches and exports them. Records of
finished inventory refreshes and drift detections, and of no operation, are
pruned after `RDSC_EXECUTION_RECORD_RETENTION_DAYS` (30 by default); every
other operation keeps its records. Inventory snapshots are pruned the same way
(see the README's "Control database").
