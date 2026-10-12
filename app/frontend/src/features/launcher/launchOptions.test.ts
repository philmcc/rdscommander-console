import { describe, expect, it } from "vitest";
import type { DefinitionSummary, ResourceRecord } from "@/api/types";
import {
  initialOptionsFor,
  launcherMatchScore,
  needsResource,
  orderGroupsByBestMatch,
} from "./launchOptions";

function definition(kind: string, properties: Record<string, unknown>): DefinitionSummary {
  return {
    kind,
    title: kind,
    group: "g",
    summary: "",
    applies_to: ["instance"],
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: false,
    needs_target: false,
    pauses_for_approval: true,
    pauses_for_approval_when: null,
    options_schema: { type: "object", properties },
  };
}

const resource = {
  kind: "instance",
  arn: "arn:aws:rds:eu-west-2:0:db:demo-orders",
  identifier: "demo-orders",
  environment_slug: "sandbox",
  target_key: "sandbox/0/eu-west-2",
} as ResourceRecord;

const hiddenResourceArn = { type: "string", ui: { widget: "hidden", help: "", order: 1 } };

describe("needsResource", () => {
  it("is true when the options carry a hidden resource_arn", () => {
    expect(needsResource(definition("drift_resolve", { resource_arn: hiddenResourceArn }))).toBe(true);
  });

  it("is false for a resource_arn the operator fills in, and for options without one", () => {
    const visible = { type: "string", ui: { widget: "text", help: "", order: 1 } };
    expect(needsResource(definition("x", { resource_arn: visible }))).toBe(false);
    expect(needsResource(definition("x", { reset: { type: "array", ui: { widget: "hidden" } } }))).toBe(
      false,
    );
    expect(needsResource({ ...definition("x", {}), options_schema: {} })).toBe(false);
  });
});

describe("initialOptionsFor", () => {
  it("fills a hidden resource_arn with the chosen resource", () => {
    expect(
      initialOptionsFor(definition("drift_resolve", { resource_arn: hiddenResourceArn }), resource),
    ).toEqual({
      resource_arn: resource.arn,
    });
  });

  it("selects the chosen resource for adoption, in its target", () => {
    expect(initialOptionsFor(definition("terraform_adoption", {}), resource)).toEqual({
      target_key: "sandbox/0/eu-west-2",
      resource_arns: [resource.arn],
    });
  });

  it("starts other tasks empty", () => {
    expect(initialOptionsFor(definition("parameter_group_edit", {}), resource)).toEqual({});
    expect(
      initialOptionsFor(
        definition("parameter_group_edit", { reset: { type: "array", ui: { widget: "hidden" } } }),
        resource,
      ),
    ).toEqual({});
  });
});

describe("initialOptionsFor a switch the task turns on or off", () => {
  // A switch that starts "off" whatever the resource has hid the current setting, and an untouched switch is
  // sent as shown; so these tasks start from the resource's current values.
  const withFacts = (facts: Record<string, unknown>) => ({ ...resource, facts }) as ResourceRecord;

  it("starts Multi-AZ and deletion protection at the resource's current setting", () => {
    expect(initialOptionsFor(definition("multi_az_change", {}), withFacts({ multi_az: true }))).toEqual({
      multi_az: true,
    });
    expect(
      initialOptionsFor(
        definition("deletion_protection_change", {}),
        withFacts({ deletion_protection: true }),
      ),
    ).toEqual({ deletion_protection: true });
  });

  it("starts the auto minor upgrade switch at the resource's current setting", () => {
    expect(
      initialOptionsFor(
        definition("auto_minor_upgrade_change", {}),
        withFacts({ auto_minor_version_upgrade: false }),
      ),
    ).toEqual({ auto_minor_version_upgrade: false });
  });

  it("starts monitoring at the current Performance Insights and enhanced monitoring settings", () => {
    expect(
      initialOptionsFor(
        definition("monitoring_change", {}),
        withFacts({ performance_insights_enabled: true, monitoring_interval: 60 }),
      ),
    ).toEqual({ performance_insights_enabled: true, monitoring_interval_seconds: 60 });
  });

  it("leaves out a setting the inventory does not know", () => {
    expect(initialOptionsFor(definition("monitoring_change", {}), withFacts({}))).toEqual({});
  });
});

describe("initialOptionsFor monitoring keeps the enhanced monitoring role", () => {
  // Without the role, turning Performance Insights on for an instance with enhanced monitoring was refused
  // with "enhanced monitoring needs a monitoring role ARN" for a setting the operator never touched.
  const monitoringRoleArn = "arn:aws:iam::123456789012:role/rds-monitoring-role";
  const withFactsAndRaw = (facts: Record<string, unknown>, raw: Record<string, unknown>) =>
    ({ ...resource, facts, raw }) as ResourceRecord;

  it("pre-fills the monitoring role of an instance whose enhanced monitoring is on", () => {
    expect(
      initialOptionsFor(
        definition("monitoring_change", {}),
        withFactsAndRaw(
          { performance_insights_enabled: false, monitoring_interval: 60 },
          { MonitoringInterval: 60, MonitoringRoleArn: monitoringRoleArn },
        ),
      ),
    ).toEqual({
      performance_insights_enabled: false,
      monitoring_interval_seconds: 60,
      monitoring_role_arn: monitoringRoleArn,
    });
  });

  it("pre-fills no role for an instance whose enhanced monitoring is off", () => {
    expect(
      initialOptionsFor(
        definition("monitoring_change", {}),
        withFactsAndRaw(
          { performance_insights_enabled: true, monitoring_interval: 0 },
          { MonitoringInterval: 0, MonitoringRoleArn: monitoringRoleArn },
        ),
      ),
    ).toEqual({ performance_insights_enabled: true, monitoring_interval_seconds: 0 });
  });
});

describe("initialOptionsFor backup settings", () => {
  // The "Copy tags to snapshots" switch read off whatever AWS had; the form now starts from the resource.
  it("starts at the resource's retention, backup window and tag copying", () => {
    const instance = {
      ...resource,
      facts: { backup_retention_period: 7, preferred_backup_window: "03:00-03:30" },
      raw: { BackupRetentionPeriod: 7, PreferredBackupWindow: "03:00-03:30", CopyTagsToSnapshot: true },
    } as ResourceRecord;
    expect(initialOptionsFor(definition("backup_settings_change", {}), instance)).toEqual({
      backup_retention_period_days: 7,
      preferred_backup_window: "03:00-03:30",
      copy_tags_to_snapshot: true,
    });
  });

  it("leaves out a backup setting the resource does not report", () => {
    const instance = { ...resource, facts: { backup_retention_period: 0 }, raw: {} } as ResourceRecord;
    expect(initialOptionsFor(definition("backup_settings_change", {}), instance)).toEqual({
      backup_retention_period_days: 0,
    });
  });
});

describe("initialOptionsFor the settings forms that opened empty", () => {
  // In the walkthrough these forms opened empty, so the operator had to look up and retype the current values.
  const cases: {
    kind: string;
    facts: Record<string, unknown>;
    raw?: Record<string, unknown>;
    expected: Record<string, unknown>;
  }[] = [
    {
      kind: "maintenance_window_change",
      facts: { preferred_maintenance_window: "sun:03:00-sun:03:30" },
      expected: { preferred_maintenance_window: "sun:03:00-sun:03:30" },
    },
    {
      kind: "tags_change",
      facts: { tags: { team: "orders", "rdscommander:managed": "true", "aws:cloudformation:stack": "s" } },
      expected: { set_tags: { team: "orders" } },
    },
    {
      kind: "storage_change",
      facts: { allocated_storage_gb: 100, max_allocated_storage_gb: 500, storage_type: "gp3", iops: 3000 },
      raw: { StorageThroughput: 125 },
      expected: {
        allocated_storage_gb: 100,
        max_allocated_storage_gb: 500,
        storage_type: "gp3",
        iops: 3000,
        storage_throughput: 125,
      },
    },
    {
      kind: "instance_class_change",
      facts: { db_instance_class: "db.t4g.micro" },
      expected: { instance_class: "db.t4g.micro" },
    },
    {
      kind: "serverless_capacity_change",
      facts: { serverless_v2_min_capacity: 0.5, serverless_v2_max_capacity: 4 },
      expected: { min_capacity_acu: 0.5, max_capacity_acu: 4 },
    },
  ];

  it.each(cases)("starts $kind at the resource's current values", ({ kind, facts, raw, expected }) => {
    const subject = { ...resource, facts, raw: raw ?? {} } as ResourceRecord;
    expect(initialOptionsFor(definition(kind, {}), subject)).toEqual(expected);
  });

  it.each(cases)("leaves $kind empty when the resource reports none of its values", ({ kind }) => {
    expect(
      initialOptionsFor(definition(kind, {}), { ...resource, facts: {}, raw: {} } as ResourceRecord),
    ).toEqual({});
  });

  it("leaves out tags when only console and AWS tags are set, and a storage type the form does not offer", () => {
    expect(
      initialOptionsFor(definition("tags_change", {}), {
        ...resource,
        facts: { tags: { "rdscommander:managed": "true" } },
      } as ResourceRecord),
    ).toEqual({});
    expect(
      initialOptionsFor(definition("storage_change", {}), {
        ...resource,
        facts: { allocated_storage_gb: 20, storage_type: "standard" },
      } as ResourceRecord),
    ).toEqual({ allocated_storage_gb: 20 });
  });
});

describe("launcherMatchScore", () => {
  it("ranks the title before the summary and never matches scattered letters", () => {
    expect(launcherMatchScore("Reboot pending instances", "reboot")).toBe(1);
    expect(launcherMatchScore("Edit parameters", "param")).toBe(0.9);
    expect(
      launcherMatchScore("Edit parameters", "reboot", ["Parameter groups", "reboot-needing parameters"]),
    ).toBe(0.5);
    expect(
      launcherMatchScore("Adopt resources", "reboot", ["Bring existing resources under Terraform"]),
    ).toBe(0);
    expect(launcherMatchScore("Edit parameters", "groups edit", ["Parameter groups"])).toBe(0.3);
    expect(launcherMatchScore("demo-orders instance arn:aws:rds:eu-west-2:1:db:demo-orders", "orders")).toBe(
      0.9,
    );
    expect(launcherMatchScore("anything", "  ")).toBe(1);
  });
});

describe("launcherMatchScore for a word written as one", () => {
  it("matches a title whose words the operator ran together above a summary that mentions them", () => {
    // In the walkthrough "failover" put Reboot (whose summary says "failover") first and never listed Fail over.
    const failOver = launcherMatchScore("Fail over", "failover", [
      "Instances and clusters",
      "Promote a reader.",
    ]);
    const reboot = launcherMatchScore("Reboot", "failover", [
      "Instances and clusters",
      "with or without failover.",
    ]);
    expect(failOver).toBeGreaterThan(reboot);
    expect(launcherMatchScore("Take snapshot", "takesnap")).toBeGreaterThan(0);
  });
});

describe("orderGroupsByBestMatch", () => {
  function entry(title: string, group: string, summary: string): DefinitionSummary {
    return { ...definition(title.toLowerCase().replaceAll(" ", "_"), {}), title, group, summary };
  }

  const adopt = entry(
    "Adopt resources",
    "Discovery and adoption",
    "Bring them under Terraform with an import.",
  );
  const refresh = entry("Refresh inventory", "Discovery and adoption", "Read everything.");
  const importEnvironment = entry("Import environment", "Environments", "Add an AWS account.");
  const edit = entry("Edit parameters", "Parameter groups", "Set values.");
  const groups: [string, DefinitionSummary[]][] = [
    ["Discovery and adoption", [adopt, refresh]],
    ["Environments", [importEnvironment]],
    ["Parameter groups", [edit]],
  ];

  it("puts the group holding the best match first, whatever the catalogue order", () => {
    expect(orderGroupsByBestMatch(groups, "import").map(([group]) => group)).toEqual([
      "Environments",
      "Discovery and adoption",
      "Parameter groups",
    ]);
  });

  it("keeps catalogue order for groups that match equally, and when nothing is typed", () => {
    expect(orderGroupsByBestMatch(groups, "").map(([group]) => group)).toEqual([
      "Discovery and adoption",
      "Environments",
      "Parameter groups",
    ]);
    // "o" is inside both groups' titles (0.8) and only in the third's group name (0.5).
    expect(orderGroupsByBestMatch(groups, "o").map(([group]) => group)).toEqual([
      "Discovery and adoption",
      "Environments",
      "Parameter groups",
    ]);
  });
});
