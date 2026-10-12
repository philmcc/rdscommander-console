# Database platform console

A local web console that manages this company's PostgreSQL estate on AWS (RDS for
PostgreSQL and Aurora PostgreSQL). It discovers what exists, brings it under Terraform
with reviewed plans, changes it through reviewed and approved operations, and keeps a
complete record of everything it ever ran.

- `docs/getting-started.md`: install, configure, start.
- `docs/operating-model.md`: how operations, approvals, and Terraform workspaces work.
- `docs/iam/rdscommander-policy.json`: the IAM permissions the console needs.
- `config.example/`: the shape of `config/` in this repository.

The console binds to 127.0.0.1 only. It commits Terraform changes into this repository
but never pushes; you push after reviewing.

## Pages

- **Overview**: the first-steps checklist, counts by kind, ownership and engine version,
  what needs attention, and the operations running now.
- **Estate**: Instances, Clusters, Parameter groups and Snapshots as dense lists with a
  filter box, sortable columns, and arrow keys and Enter to open a row; at phone width
  each row becomes a card.
- **Resource pages**: facts, relationships, parameters, snapshots, pending maintenance,
  drift, recent operations and change history, with an Actions menu that offers only what
  the resource and the environment's mode allow and says why the rest is unavailable.
- **Operations** and **History**: every operation with its steps and diagnostics, every
  execution record (Terraform, AWS API, git), and the change history with an
  outside-the-console filter and a review mark.
- **Needs attention** (the bell): operations awaiting approval, parameter changes pending
  a reboot, pending maintenance, drift, changes made outside the console, recent failures,
  the blue side kept by a major upgrade, and, in Terraform-mode environments, resources not yet under Terraform; every row carries
  the action that resolves it.
- **Environments**: one page per environment, the import wizard, and side-by-side
  comparison of two environments.
- **Settings**: operator, console version, theme (system, light or dark) and the
  environments' configuration.

Start any task from the Actions menu, the inbox, or the launcher (⌘K or "Start a task").
Every task opens the task panel: options, then a review (the Terraform plan or the change
set, with reboot and downtime stated), then approval, then progress. Destructive and
disruptive tasks ask you to type the resource's identifier before they run.

## Change-management modes

Each environment sets `change_management` in `config/environments.yaml`. In `terraform`
mode the console renders the Terraform files, plans, shows the plan for review, applies the
approved plan, checks that a second plan is clean, commits, and refreshes the target's
inventory, so the next change starts from what AWS now has (if that refresh fails the
committed change stands, and the next change on the target waits until you run Refresh
inventory); resources must be adopted
(Adopt resources) before they can be changed, and an adoption must include the parameter
and subnet groups an instance or cluster uses unless they are adopted already. In `direct`
mode the console calls the AWS API after review, reads the values back until AWS reports
them, refreshes the resource, and then writes the Terraform files and `console-record.json`
as the record of what changed. Either way every change is one commit named for what it
did, who did it and who approved it: `<task>: <resource> (<operator>, approved by
<approver>)`, and for an adoption `Adopt <n> resources into Terraform:
<environment>/<account>/<region> (<operator>, approved by <approver>)` with the adopted
addresses in the body. A commit names an approver only when one approved; accepting drift
has no approval pause, so its commit reads `Resolve drift: <resource> (<operator>)`.

### Switching modes

Changing the mode is an edit to `config/environments.yaml` followed by a restart. The next
Detect drift brings the record in line with the new mode in one commit, before it compares
anything:

- **Terraform to direct**: direct mode records every resource. The resources the record
  lacked (everything that was never adopted) join it from the latest inventory in one
  `Record estate: <environment> (<operator>)` commit, instead of each becoming a "live, not
  recorded" finding. What was recorded keeps its recorded values, so its drift stays visible.
- **Direct to Terraform**: Terraform mode records only what is under Terraform. The
  recorded resources that are not adopted leave the record and the Terraform files in one
  `Record adopted resources: <environment> (<operator>)` commit; from then on detection
  compares only adopted resources. Adopt the rest when you want Terraform to manage them.

In direct mode the `Record estate` step is not only for a switch: every Detect drift runs
it for the live resources the record lacks. A resource created outside the console
therefore joins the record at the next detection, in a `Record estate` commit, rather than
becoming a "live, not recorded" finding. Its creation stays visible as an outside change:
the refresh that first saw it recorded the creation in the resource's history and in
History → Changes, and the inbox lists it until someone marks it reviewed.

"Adopted" means under Terraform: the console adopted the resource, or Terraform state holds
it (a Terraform-mode change created it, or a console with another control database adopted
it; detection and adoption read the state object to learn this). The `rdscommander:managed`
tag is not evidence, so a group the console created in direct mode is offered for adoption
once the environment is in Terraform mode. Both commits run the checkout guard first and put
the files back when git refuses the commit, as the first record of a direct-mode estate does.

## Importing an environment

Environments → Import environment adds an AWS account and its regions as a new
environment, from a profile in `~/.aws/config` on this machine or from the simulated
account. Choose a slug, a display name, whether it is production, the regions (or none to
scan every enabled region and keep those with PostgreSQL), and the change-management mode.
The console confirms the account, writes `config/environments.yaml`, commits it as
`Import environment: <slug> (<operator>)`, and queues the first inventory refresh. The new
environment appears in the switcher at once.

## Parameter groups

Create a group from the family defaults or by cloning another group, with overrides
validated against the family as you type. Edit parameters shows each parameter with its
default, type and apply type, marks the ones set on the group, and flags those that need a
reboot. Compare a group with the family defaults or with another group, attach a group to
an instance or cluster (optionally rebooting now, which asks for the identifier), delete a
group nothing uses, and reboot the instances whose parameters are pending a reboot. Build
upgrade parameter groups creates the groups for a target engine version: it classifies each
override as carried, default changed, needs review, removed or renamed, lets you change any
value before approving, and records on each new group's page where it came from and what
was changed on the review.

Values are checked against the parameter metadata of the group's family and scope: Aurora
lists different parameters for instance groups and cluster groups. A real account reads the
metadata from AWS (`DescribeEngineDefaultParameters` and
`DescribeEngineDefaultClusterParameters`). The simulated account uses metadata bundled with
the console, whose split of the Aurora families is an approximation: `shared_buffers` and
`max_connections` are instance-only, `rds.force_ssl` and `rds.logical_replication`
cluster-only, and every other bundled parameter is offered in both scopes.

## Instances, clusters and snapshots

Every task below starts from the resource's Actions menu (or the launcher), except the two
creates, which start from the environment's page, and takes the
same path: options, review, approval, progress. "Modes" says where it is offered; "Approval"
says what the approver types. Every task pauses for approval in both modes.

| Operation | Applies to | Modes | Approval | What changes |
|---|---|---|---|---|
| Reboot | Instance, parameter group (every instance pending a reboot) | Terraform, direct | Identifier | Reboots any available instance, with or without failover; nothing in the rendered files changes. |
| Change instance class | Instance | Terraform, direct | Identifier | The instance class; the instance restarts (Multi-AZ fails over). |
| Change storage | Standalone instance | Terraform, direct | Click | Allocated storage, storage type, IOPS, throughput and the autoscaling ceiling. |
| Change Multi-AZ | Standalone instance | Terraform, direct | Click | Adds or removes the standby in another availability zone. |
| Change backup settings | Instance, cluster | Terraform, direct | Click | Automated backup retention, the backup window, whether snapshots copy tags. |
| Change maintenance window | Instance, cluster | Terraform, direct | Click | The weekly window in which AWS applies maintenance. |
| Change monitoring | Instance | Terraform, direct | Click | Performance Insights and enhanced monitoring, on or off, and their settings. |
| Change deletion protection | Instance, cluster | Terraform, direct | Click | Whether AWS refuses to delete the resource. |
| Change serverless capacity | Cluster | Terraform, direct | Click | The minimum and maximum Aurora Serverless v2 capacity (ACU). |
| Change tags | Instance, cluster (and its members), parameter group | Terraform, direct | Click | Sets and removes tags; tags starting `rdscommander:` or `aws:` are never touched. |
| Fail over | Cluster | Terraform, direct | Identifier | Promotes another member to writer; connections drop briefly. |
| Apply pending maintenance | Instance, cluster | Terraform, direct | Click; identifier when the opt-in is immediate | Opts a pending maintenance action in now or at the next window, or undoes the opt-in. |
| Take snapshot | Instance, cluster | Terraform, direct | Click | A manual snapshot with tags; the source is not changed. |
| Delete snapshot | Snapshot | Terraform, direct | Identifier | Deletes a manual snapshot; this cannot be undone. |
| Promote replica | Read replica | Terraform, direct | Identifier | A read replica becomes a standalone instance; replication stops for good and it restarts. |
| Create read replica | Instance | Terraform, direct | Click | A new replica of the instance; the source keeps running. |
| Add reader | Cluster | Terraform, direct | Click | A new Aurora reader instance in the cluster. |
| Remove reader | Instance (an Aurora reader) | Terraform, direct | Identifier | Deletes the reader; connections to it drop and the cluster keeps running. |
| Restore to new instance or cluster | Snapshot | Terraform, direct | Click | A new instance, or from a cluster snapshot a new cluster with one writer; the snapshot is not changed. |
| Point-in-time restore | Instance, cluster | Terraform, direct | Click | A new instance or cluster as it was at a time inside the backup window (or the latest restorable time); the source keeps running. |
| Create instance | Environment | Terraform, direct | Click | A new PostgreSQL instance from the template: encrypted, backed up for 7 days, protected from deletion. |
| Create cluster | Environment | Terraform, direct | Click | A new Aurora PostgreSQL cluster with a writer and readers from the same template. |
| Decommission | Standalone instance without replicas, cluster | Terraform, direct | Identifier | Deletes an instance, or a cluster with its members, after a final snapshot. This cannot be undone. |

### How these tasks apply

- **Settings changes** (class, storage, Multi-AZ, backup settings, maintenance window,
  monitoring, deletion protection, serverless capacity, tags) apply now. Five of them
  (instance class, storage, Multi-AZ, backup settings and monitoring) also offer "apply at
  the next maintenance window". **Apply at the window is direct-mode only**: in Terraform
  mode a window request is refused (`apply_at_window_not_available`), because Terraform
  applies what the files say when it runs; choose "apply now". Only the settings AWS can
  hold until the window are queued for it; the review says which.
- **Direct actions** (reboot, fail over, apply pending maintenance, take and delete a
  snapshot, promote a replica) run the same AWS calls in both modes and end
  by refreshing the resource. When the action changes a rendered attribute (a promoted replica
  is no longer a replica) they also reconcile: in Terraform mode the files are rendered, the
  state refreshed, a second plan checked for no changes, and the result committed; in direct
  mode the files are rendered and committed.
- **Removing a reader** is a change like the others: Terraform mode renders, plans, applies
  a plain delete and commits; direct mode deletes the instance through the API, refreshes
  and commits the record.
- **Reboot** applies to any available instance, not only one with pending parameters.
- **Pending maintenance** is read from AWS, so it needs a real account; the simulated
  account cannot show it.
- **Creating and restoring**: the master password of a created instance or cluster is kept
  by AWS Secrets Manager and never passes through the console, so the console's role needs
  the Secrets Manager statement and `kms:DescribeKey` in
  `docs/iam/rdscommander-policy.json`. Enhanced monitoring (on a change, create, replica or
  restore) passes the monitoring role to RDS, so the role needs `iam:PassRole` limited to
  `monitoring.rds.amazonaws.com`; restoring or replicating from a source encrypted with a
  customer-managed KMS key needs `kms:CreateGrant` through RDS (and the key's own policy must
  allow the console's role). Both statements are in the same policy file. A direct-mode create sends what the rendered files
  carry, followed by a modify for settings the create call cannot take. A Terraform-mode
  create ends with a second commit, "(record as built)", that records the resource as AWS
  built it. A new cluster's readers depend on its writer. A read replica cannot be created
  from an instance whose master password Secrets Manager manages. A restored or created
  resource keeps its create-only arguments (the snapshot source, the restore block, the
  managed password) in the record, and drift ignores them.
- **Decommission** is two phases under one approval: deletion protection is turned off, then
  the resources are deleted with a final snapshot. In Terraform mode that is two applies and
  two commits.
- **Tags** apply to instances, cluster members, clusters and parameter groups.
- A change to a resource with open record drift is refused until the drift is resolved, and
  a resource owned by other tooling is read-only unless the environment allows direct
  changes to it. In Terraform mode a resource must be adopted before it can be changed.

### Known limits of the simulated account

The simulator cannot show Performance Insights, pending maintenance, a promoted replica's
cleared source, or the Terraform clean-plan check after a create. The verification notes
`docs/verification-results.md` lists how each is covered instead.

## Upgrades

Four tasks, in the Actions menu's "Upgrades" group, change a resource's engine version or look after what an
upgrade leaves behind. They apply to instances and clusters; an Aurora cluster member runs the version of its
cluster, so the menu says to upgrade the cluster instead. Like every other task they pause for approval in both
modes.

| Task | Approval | What it does |
|---|---|---|
| Change auto minor upgrade | Click | Turns on or off whether AWS applies new minor versions on its own in the maintenance window. Offered on instances, Aurora members and clusters. |
| Minor version upgrade | Identifier | Moves the instance or cluster to a newer minor version of the same major version. The instance restarts (Multi-AZ fails over). |
| Major version upgrade | Click, then the identifier | Moves the instance or cluster to a new major version through an RDS blue/green deployment. Production keeps running on the old version until the second approval. |
| Clean up blue/green deployment | Identifier | Deletes the deployment a failed or rejected major upgrade left in place, and its green copy unless you keep it. |

### Minor upgrade and auto minor upgrade

The target must be a version the engine-versions table lists as a minor upgrade of the current one (the table
comes from AWS on a real account and from a bundled list on the simulated account); anything else is refused
before approval, naming the listed targets. In direct mode the console sends only `EngineVersion` (with
`ApplyImmediately` for "apply now", or held for the next maintenance window), never `AllowMajorVersionUpgrade`,
and waits until the resource reports the target version and is available. For a cluster the change goes to the
cluster and its members follow; the console records the members at the new version as well.

In Terraform mode the new `engine_version` is rendered and applied. The templates ignore `engine_version`
changes while auto minor upgrade is on, so when it is on the upgrade also turns it off for the apply; the review
says so, and the setting stays off afterwards. Turn it back on with "Change auto minor upgrade" if you want AWS
to keep applying minor versions. Restoring a recorded `engine_version` from drift is refused: the console does
not downgrade.

### Major upgrade through a blue/green deployment

A major upgrade never changes production in place. It asks AWS for a blue/green deployment: a green copy of the
instance or cluster on the target version, kept in step with production, which you can test before anything
moves. Two approvals bracket it. The steps are:

Nothing changes in AWS or in the repository before the first approval.

1. **Choose the target and plan the green parameter groups.** The target must be a major upgrade the
   engine-versions table lists for the current version; the target's parameter group family is worked out from
   the table. The green groups are planned from the latest inventory, without creating anything: either name
   existing groups of the target family (checked against the inventory, so refresh it after creating one), or
   let the console build them. Building first looks for groups of the target family that the upgrade parameter
   group builder already made from this resource's current groups; only if every current group has one are
   they reused. Otherwise the console works out the names the builder will give the new groups.
2. **Preflight.** Eight checks run and each appears on the review's checklist as pass, warning or failure; any
   failure stops the upgrade before the deployment is created.
   - *Pending maintenance*: maintenance AWS has queued for the resource or its members could interfere (warning).
   - *Automated backups*: a blue/green deployment needs a retention of at least one day (failure otherwise).
   - *Operations in progress*: another console operation on the resource or a member that is queued, running or
     awaiting approval (failure).
   - *Read replicas*: replicas are listed; they are not carried to green and must be created again afterwards
     (warning).
   - *Instance class and storage*: each instance class, and for an instance its storage type, must be orderable
     on the target version (failure otherwise).
   - *Extensions*: the console cannot read the installed extensions, so this always warns; compare them with the
     target version before switching over.
   - *Name of the blue side*: AWS keeps the old resource as `<identifier>-old1`; if that name already exists
     (usually an earlier upgrade's blue side) the check fails until it is decommissioned.
   - *Logical replication*: a blue/green deployment keeps the green side in step through logical replication,
     so `rds.logical_replication` must be `1` on the parameter group of the resource (an Aurora cluster's
     cluster parameter group). Off, or left at its default of `0`, fails: set it to `1` with Edit parameters
     and reboot (it is a static parameter). A group missing from the latest inventory warns. The simulated
     estate's groups have it on.
3. **First approval: create the green side.** The review names the groups and says whether the upgrade will
   build them, reuses them or uses existing ones, and that production is not touched until the switchover.
   Nothing is typed, because creating the green side interrupts nothing.
4. **Prepare the green parameter groups.** When the plan builds them, the builder runs now, inside the upgrade
   as an operation of its own approved with it (it carries the approver and time of the first approval), in
   the environment's mode (Terraform plan and commit, or direct API calls with the files committed), and its
   groups are committed before the deployment is created. Reused and existing groups need nothing.
5. **Create the deployment** (`CreateBlueGreenDeployment`), at most once; a retry after a crash finds the
   deployment by its name, `<identifier>-pg<major>-<8 characters of the operation id>`.
6. **Wait for the green side.** The console polls until AWS reports the deployment `AVAILABLE`. Copying a large
   database takes hours, so this wait has its own limit, `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS` (four hours by
   default).
7. **Second approval: switch over.** The operation pauses again. It first checks that the deployment is still
   `AVAILABLE` and that production has no modifications pending, failing (and leaving the deployment) if not,
   then shows the green endpoint to test against and what the switchover does. It also fails while another
   console operation on the resource (or a member) is queued, running or awaiting approval, and the review
   names every console operation that changed production after the green copy was made ("changed on the blue
   side since the green copy was made"), with a warning: the green side does not carry those changes. The
   approver types the production identifier. The same checks run again right before the switchover, which may
   be hours later.
8. **Switch over** (`SwitchoverBlueGreenDeployment`, with AWS's five-minute switchover timeout; AWS rolls back
   when it takes longer). Connections drop while AWS switches over, usually for under a minute. Production now
   runs the green side under the original name and endpoint.
9. **Record the blue side.** The old resource, renamed by AWS to `<identifier>-old1`, is checked against the
   resource id captured before the switchover, tagged `rdscommander:blue-side=<operation id>`, and inventory
   is refreshed, so it is listed even if a later step fails.
10. **Verify** that production reports the target version and the green parameter groups (for a cluster, each
    member's group too). A mismatch is recorded as a warning on the operation and the upgrade still succeeds,
    because the switchover has happened and the record must follow AWS; only an instance AWS no longer lists
    fails the step.
11. **Reconcile** the rendered files with what AWS now has and commit. In Terraform mode, for a standalone
    instance, the console moves the old instance's state entry to `<identifier>_old1`, imports the green
    instance into the production address, and requires a clean plan before committing. An Aurora cluster and
    its members are tracked by identifier, so no state is moved for them.

**The blue side is never deleted by the upgrade.** The old instance (or cluster) stays, under its `-old1` name,
and the inbox lists it as "<identifier> is the blue side of a major upgrade" with the Decommission action, which
asks for the identifier and takes a final snapshot like any decommission. In Terraform mode the standalone
blue-side instance is already console-managed, so Decommission is offered at once; a blue-side Aurora cluster
must be adopted first. The deployment itself stays in AWS in the state `SWITCHOVER_COMPLETED`; the console does
not delete it.

**Passing AWS failures do not end the upgrade.** While the console waits for the green side, polls the
switchover, or waits for a cleanup to settle, a throttled call, an expired login (renew it meanwhile) or a lost
connection is logged as a warning and asked again with a backoff (doubling, at most a minute) until that wait's
own limit; only then does the step fail.

**A failure at any step after the deployment exists leaves it in place.** The diagnostic names the deployment,
the AWS error and the step, and says what is left: before the switchover production is unchanged, and after it
production runs the green side and the blue side is kept. Rejecting the first approval creates nothing in AWS
or in the repository; rejecting the switchover approval leaves the deployment in place, and a
warning on the operation says it and its green side still run. Change history credits the green side, and after
a switchover the renames, to the upgrade.

### Cleaning up a deployment that was never switched over

"Clean up blue/green deployment" is the only task that deletes a deployment. It is offered on the resource, and
by the inbox on a failed or rejected major upgrade, while that upgrade's deployment is left in place and has not
switched over; a deployment that is switching over or has switched over is refused, because production already
runs on the green side. The review names the deployment, the status AWS reports for it now and the green
instances, and the approver types the production identifier. Right before deleting, the console reads the
status again: a deployment that started switching over after the review is refused and nothing is deleted.
"Delete the green side" is on by default; turn it off
to keep the green copy as standalone databases that belong to no deployment. Production is never touched. A
deployment AWS no longer knows is recorded as deleted.

### Permissions for upgrades

`docs/iam/rdscommander-policy.json` grants the console `rds:CreateBlueGreenDeployment`,
`rds:DescribeBlueGreenDeployments`, `rds:SwitchoverBlueGreenDeployment` and `rds:DeleteBlueGreenDeployment`, and
the reads the preflight uses (`rds:DescribeDBEngineVersions`, `rds:DescribeOrderableDBInstanceOptions`,
`rds:DescribePendingMaintenanceActions`). A blue/green deployment also makes RDS act with the caller's
permissions to build and tag the green side, so the role needs `rds:CreateDBInstance`,
`rds:CreateDBInstanceReadReplica`, `rds:ModifyDBInstance`, `rds:CreateDBParameterGroup`,
`rds:AddTagsToResource`, `rds:DescribeDBInstances` and `rds:PromoteReadReplica`, and for Aurora
`rds:CreateDBCluster`, `rds:CreateDBClusterEndpoint` and `rds:PromoteReadReplicaDBCluster` (the switchover
promotes the green cluster). All of these are in the policy file; `rds:CreateDBClusterEndpoint` has a
statement of its own, `BlueGreenGreenSideEndpoints`. If the role is narrower than the shipped policy, AWS refuses
the create or leaves the deployment `INVALID_CONFIGURATION`; the upgrade fails with the AWS reason on the
diagnostic, and a deployment that exists is left for cleanup.

### What the simulated account cannot show

- Replication lag and the green side's data: a simulated deployment is available and switches over at once, so
  the waits and the pause for testing are exercised for one poll only.
- Orderable options, pending maintenance and extensions: the simulator lists no orderable options (the class and
  storage check warns instead of checking), has no pending maintenance, and runs no databases to inspect.
- An Aurora cluster's switchover: the simulator renames the cluster to `-old1` but not its members, so an Aurora
  major upgrade fails at verification there; it needs a sandbox account. Every simulated instance also shares one
  resource id, so the blue-side identity check passes trivially.
- A minor upgrade passes through no `upgrading` status, and maintenance-window queuing is not simulated.
- In Terraform mode the state move and re-import are verified against a scripted runner only.

Run a major upgrade in a sandbox AWS account, and read the AWS reasons it reports, before using it on client
estates.

## Change history and drift

Inventory is refreshed every `inventory_refresh_minutes` (15 by default) and from the
Refresh inventory button. Each refresh compares the new snapshot with the previous one and
records every changed attribute on the resource's page and in History → Changes, either as
made by the console (linked to the operation) or as made outside the console, which also
appears in the inbox until someone marks it reviewed. A change queued for the maintenance
window is linked to the operation that queued it when AWS applies it, days later. Detect drift (scheduled every
`drift_check_hours`, and in the launcher) compares the live estate with the committed
record; each drifted resource can be resolved by accepting what AWS has into the record or
by restoring the recorded values to AWS through the normal review. A change to a resource
whose drift is not resolved is refused with "Resolve drift on <resource> first", so drift is
never folded silently into an approved change. In both modes a change refreshes its
target's inventory, so the next change is worked out from what AWS has and the resource's
page and history show the change when the operation finishes: in Terraform mode as the last
step, after the commit; in direct mode before the record step, which records from that
refresh. An adoption refreshes the same way once it commits, so the adopted resources show
as adopted at once.

## Control database

The control database (Postgres) holds operations with their steps, diagnostics and execution
records, the inventory snapshots, the change history and the drift findings. The inventory
is what grows: every refresh of a target (every `inventory_refresh_minutes`, 15 by default,
so 96 a day) stores a snapshot with every instance, cluster, subnet group, DB snapshot and
parameter group of the target, every parameter of every group (a few hundred per group,
defaults included), and an execution record per AWS call. After each refresh the console
prunes:

- the target's snapshots beyond the newest `RDSC_INVENTORY_SNAPSHOT_RETENTION_PER_TARGET`
  (96 by default, a day at the default cadence), with every row that hangs off them, except
  the snapshots a change event (before and after each change), a drift finding (the
  snapshot the detection compared) or an operation (the snapshot a Terraform-mode change
  rendered from) points at;
- execution records older than `RDSC_EXECUTION_RECORD_RETENTION_DAYS` (30 by default) that
  belong to no operation, or to a finished inventory refresh or drift detection. Every other
  operation keeps its execution records for good.

Each prune is logged as `inventory_snapshots_pruned` or `execution_records_pruned` with
its counts. As a rough guide, a target with 20 parameter groups stores about 20 × 400 =
8,000 parameter rows per snapshot, so about 800,000 at the default retention, plus the
snapshots the change history keeps (two per change). Operations, change events and drift
findings are never pruned.
