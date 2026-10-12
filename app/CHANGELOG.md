# Changelog

## 0.4.0

- Upgrades: four new operation kinds in an "Upgrades" group: `auto_minor_upgrade_change` (turn AWS's automatic minor upgrades on or off), `minor_upgrade` (move to a newer minor version of the same major version), `major_upgrade` and `blue_green_cleanup`. The catalogue now lists 37 kinds.
- Major upgrade through an RDS blue/green deployment: target and parameter groups chosen or built for the target family, seven preflight checks, a first approval to create the green side, a wait for it, a second approval typed by the production identifier, the switchover, verification and reconcile. The old (blue) side is kept as `<identifier>-old1`, tagged `rdscommander:blue-side`, and listed in the needs-attention inbox with Decommission. It is never deleted automatically.
- A failure or rejection at any blue/green step leaves the deployment in place with a diagnostic naming the AWS reason; `blue_green_cleanup` is the only operation that deletes a deployment, and the inbox offers it on a failed or rejected upgrade.
- Minor upgrade in Terraform mode: when auto minor upgrade is on, the upgrade turns it off so the new `engine_version` can be applied, says so in the review, and leaves it off.
- Migration `0003_blue_green_deployments` adds the table that records each blue/green deployment (its id, the operation, the resource, status, green endpoint, switchover and deletion times).
- New setting `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS` (default 14400): how long a major upgrade waits for the green side to become available.
- IAM: the policy gains `rds:CreateBlueGreenDeployment`, `rds:DescribeBlueGreenDeployments`, `rds:SwitchoverBlueGreenDeployment` and `rds:DeleteBlueGreenDeployment`, and `rds:CreateDBClusterEndpoint` (in its own `BlueGreenGreenSideEndpoints` statement), which RDS uses on the caller's behalf to build an Aurora green side. The reads the preflight and the target lists use, and the other permissions a deployment needs on the caller's behalf, were already granted.
- Drift restore refuses to restore an `engine_version`, so the console never offers a downgrade.
- Simulator limits: the simulated account cannot show replication lag or the green side's data, orderable instance options, pending maintenance, an Aurora cluster's member renames at switchover (so an Aurora major upgrade fails verification there), or an `upgrading` status. The real flow needs a sandbox account before client use.

### Upgrading from 0.3.0

- Migration `0003_blue_green_deployments` runs when the console starts; nothing needs to be done by hand.
- Add the blue/green actions to the console's IAM role: take `docs/iam/rdscommander-policy.json` from this release (new actions: `rds:CreateBlueGreenDeployment`, `rds:DescribeBlueGreenDeployments`, `rds:SwitchoverBlueGreenDeployment`, `rds:DeleteBlueGreenDeployment` and `rds:CreateDBClusterEndpoint`). Without them the upgrade tasks fail with the AWS error on their diagnostic; everything else keeps working.
- Optionally set `RDSC_BLUE_GREEN_CREATE_TIMEOUT_SECONDS` in `.env`; the default of four hours suits most databases.

## 0.3.0

- Instance and cluster settings: change the instance class, storage, Multi-AZ, backup settings, maintenance window, monitoring, deletion protection, Aurora Serverless v2 capacity and tags, each reviewed in both modes.
- Reboot, failover and maintenance: reboot any available instance, fail an Aurora cluster over to another member, and apply pending maintenance now, at the next window, or undo the opt-in.
- Snapshots and restores: take and delete manual snapshots, restore a snapshot to a new instance or cluster, and restore to a point in time inside the backup window.
- Replicas and readers: create a read replica, promote one to a standalone instance, and add or remove an Aurora reader.
- Create and decommission: create an instance or an Aurora cluster from the template (encrypted, backed up, protected from deletion, master password kept in Secrets Manager), and decommission an instance or cluster with a final snapshot.
- Shared machinery: attribute changes that can apply at the maintenance window in direct mode, direct actions that refresh and reconcile, creates that record the resource as built, and two-phase changes under one approval.
- IAM: the policy gains the RDS calls for the new operations (and `rds:DescribeGlobalClusters`), the Secrets Manager statement for managed master passwords and `kms:DescribeKey`, `iam:PassRole` on the enhanced monitoring role (only to `monitoring.rds.amazonaws.com`), and `kms:CreateGrant` through RDS for customer-managed keys.
- Simulator limits: the simulated account cannot show Performance Insights, pending maintenance, a promoted replica's cleared source, or the Terraform clean-plan check after a create.

### Upgrading from 0.2.0

- The templates now render `apply_immediately` on every instance, cluster and cluster member. The first operation on each Terraform-mode target therefore also updates that line on every resource in the workspace; it is configuration only and changes nothing in AWS. The policy check lets it through and names those resources under "Also brought up to date"; this is expected.
- A resource whose master password Secrets Manager manages shows one record drift on `manage_master_user_password` after the upgrade; accept it from AWS.
- Every Aurora cluster member shows one record drift on `preferred_maintenance_window` (and on `monitoring_role_arn` and `performance_insights_retention_period` where enhanced monitoring or Performance Insights is on) after the upgrade, because the record did not carry those member settings before; accept them from AWS.
- In Terraform mode, run any change on a workspace (or accept its drift and let one operation apply) before the first Promote read replica after the upgrade: promote verifies the workspace with a plan that must be clean, and the new `apply_immediately` lines make the first plan after the upgrade non-empty.

## 0.2.0

- New console: design system with light and dark themes, sidebar navigation, resource pages with a state-aware Actions menu, a ⌘K launcher, a needs-attention inbox, and a task panel that takes every operation through options, review and approval.
- Operation catalogue: every operation is defined once with its options schema and applicability; `POST /api/v1/operations` creates any of them.
- Change-management modes per environment: `terraform` (plan and apply) or `direct` (AWS API with the Terraform files kept as the record); every change commits the files and `console-record.json`.
- Environment import from an AWS profile or the simulated account; environment pages and comparison.
- Scheduled inventory refresh, resource change history with outside-console detection, drift detection against the record with accept or restore.
- Parameter groups: create, edit with validation, compare, attach with optional reboot, delete, reboot pending instances, and the upgrade parameter group builder with carried / default changed / needs review / removed / renamed classification.
- Inventory now captures snapshots, pending maintenance and parameter apply status.
- Terraform-mode changes end with an inventory refresh, so resource pages, ownership and history show the change as soon as the operation finishes; a refresh that fails is reported without failing the committed change.

## 0.1.0

- Estate inventory across every configured environment, account, and region.
- Simulated AWS account for rehearsals with no credentials.
- Terraform adoption of parameter groups, subnet groups, clusters, and instances with
  plan review, approval, verification, and a commit into this repository.
- Complete execution history with search and export.
