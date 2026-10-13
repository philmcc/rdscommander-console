# Known limits

Every automated test and every walkthrough so far ran against a simulated AWS account (a moto server). Read
this before pointing the console at a real account.

## Prove these on a sandbox account first

- **Major upgrades through blue/green deployments.** The whole flow, in both change-management modes, has
  only run against the simulator. Specifically unproven: the Terraform-mode state move and re-import after a
  switchover (verified against a scripted runner only), Aurora major upgrades, whether AWS copies in-Region
  read replicas to the green side (the preflight currently warns that it does not), the
  `rds.logical_replication` setting together with a pending reboot, deleting a deployment after a completed
  switchover, deployments on resources whose master password Secrets Manager manages, and the IAM
  permissions a real blue/green create needs.
- **Aurora cluster detail.** Cluster members rendering `engine_version`, and the scope split between
  instance and cluster parameters, are covered by bundled metadata and approximations in the simulator. A
  real account reads both from AWS, which is authoritative.
- **Large estates.** Discovery reads every parameter of every parameter group and the tags of every
  resource, one call at a time. It retries throttling, but a first scan of an account with many groups is
  slow.

Begin with read-only discovery (attach only the reads statement of `docs/iam/rdscommander-policy.json`),
compare the inventory with the AWS console, then try small changes on a non-production target.

## What the simulator cannot show

None of these is worked around in the product.

- No pending maintenance (`DescribePendingMaintenanceActions` is not implemented), no replication lag or
  green-side data, no `upgrading` status, no orderable instance options for newer versions, and no databases
  (extension checks always warn).
- Every DB instance has the same resource id, which stops Terraform-mode changes when more than one instance
  exists, because the AWS provider waits for a modification by that id.
- Performance Insights is never reported, so a direct-mode verification of a change that turns it on waits
  for its whole timeout, and a Terraform-mode create with it on fails the clean-plan check.
- Every cluster reports a serverless v1 scaling configuration, which fails the clean-plan check of a
  Terraform-mode cluster create.
- The provider's parameter `Source` is not set on parameters Terraform creates, so with some AWS provider
  versions a Terraform-mode parameter group create fails its clean-plan check. Real AWS sets it.
- Resetting a parameter in Terraform mode calls `ResetDBParameterGroup`, which the simulator does not
  implement; the apply retries until its timeout and the runner works through one operation at a time.
- Restores copy the source instance's settings, a promoted replica keeps its source, and a read replica
  breaks the bookkeeping of a blue/green switchover (remove or promote replicas before a major upgrade there).
- The simulator reports account `123456789012` for a target configured as `000000000000`.

## Behaviours to know about

- Two environments covering the same account and region share resource identities. A resource belongs to the
  environment that listed it first, and changing it from the other environment is refused. Configure each
  account and region in one environment only.
- A failed adoption leaves its rendered files uncommitted by design. The next adoption is blocked until they
  are committed or discarded (see `getting-started.md`).
- After an adoption succeeds, the estate page shows the new ownership after the next inventory refresh.
- The console commits but never pushes.
