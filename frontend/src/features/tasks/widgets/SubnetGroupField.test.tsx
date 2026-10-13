import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WidgetProps } from "../SchemaForm";
import { SubnetGroupField } from "./SubnetGroupField";

afterEach(() => vi.unstubAllGlobals());

function subnetGroup(name: string, vpcId: string, subnetCount: number) {
  return {
    id: 1,
    db_subnet_group_arn: `arn:aws:rds:eu-west-2:0:subgrp:${name}`,
    db_subnet_group_name: name,
    description: `${name} description`,
    ownership: "unmanaged",
    subnet_ids: Array.from({ length: subnetCount }, (_, index) => `subnet-${index}`),
    tags: {},
    vpc_id: vpcId,
  };
}

function target(targetKey: string, groups: object[]) {
  return {
    target_key: targetKey,
    aws_account_id: "0",
    aws_region: "eu-west-2",
    aws_profile_name: null,
    simulated: true,
    snapshot: null,
    instances: [],
    clusters: [],
    parameter_groups: [],
    subnet_groups: groups,
  };
}

function stubInventory() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/environments/sandbox/inventory")
        return Response.json({
          environment_slug: "sandbox",
          targets: [
            target("sandbox/0/eu-west-2", [
              subnetGroup("demo-private-subnets", "vpc-0aaa", 2),
              subnetGroup("demo-public-subnets", "vpc-0aaa", 3),
            ]),
            target("sandbox/1/eu-west-1", [subnetGroup("other-account-subnets", "vpc-0bbb", 2)]),
          ],
        });
      return new Response("not found", { status: 404 });
    }),
  );
}

function renderField(overrides: Partial<WidgetProps> = {}) {
  const onChange = vi.fn();
  render(
    <SubnetGroupField
      name="subnet_group_name"
      schema={{ type: "string" }}
      ui={{ widget: "subnet_group", label: "Subnet group" }}
      value=""
      onChange={onChange}
      context={{ environmentSlug: "sandbox", targetKey: "sandbox/0/eu-west-2" }}
      formValue={{}}
      {...overrides}
    />,
  );
  return onChange;
}

describe("SubnetGroupField", () => {
  it("lists the subnet groups of the task's target and emits the chosen name", async () => {
    stubInventory();
    const onChange = renderField();
    const select = await screen.findByRole("combobox", { name: "Subnet group" });
    await screen.findByRole("option", { name: /demo-private-subnets/ });
    expect(screen.getByRole("option", { name: /demo-private-subnets/ })).toHaveTextContent(
      "vpc-0aaa, 2 subnets",
    );
    expect(screen.queryByRole("option", { name: /other-account-subnets/ })).toBeNull();
    await userEvent.selectOptions(select, "demo-public-subnets");
    expect(onChange).toHaveBeenLastCalledWith("demo-public-subnets");
  });

  it("follows the target chosen in a create form", async () => {
    stubInventory();
    renderField({
      context: { environmentSlug: "sandbox" },
      formValue: { target_key: "sandbox/1/eu-west-1" },
    });
    await screen.findByRole("option", { name: /other-account-subnets/ });
    expect(screen.queryByRole("option", { name: /demo-private-subnets/ })).toBeNull();
  });

  it("describes the chosen group", async () => {
    stubInventory();
    renderField({ value: "demo-private-subnets" });
    expect(await screen.findByText("demo-private-subnets description")).toBeInTheDocument();
  });
});

describe("SubnetGroupField when the target changes", () => {
  it("clears a group of the earlier target, and says so until another is picked", async () => {
    stubInventory();
    const onChange = vi.fn();
    function Harness() {
      const [form, setForm] = useState<Record<string, unknown>>({ target_key: "sandbox/0/eu-west-2" });
      return (
        <>
          <button type="button" onClick={() => setForm({ ...form, target_key: "sandbox/1/eu-west-1" })}>
            Other target
          </button>
          <SubnetGroupField
            name="subnet_group_name"
            schema={{ type: "string" }}
            ui={{ widget: "subnet_group", label: "Subnet group" }}
            value={form.subnet_group_name}
            onChange={(next) => {
              onChange(next);
              setForm((current) => ({ ...current, subnet_group_name: next }));
            }}
            context={{ environmentSlug: "sandbox" }}
            formValue={form}
          />
        </>
      );
    }
    render(<Harness />);
    const select = screen.getByRole("combobox", { name: "Subnet group" });
    await screen.findByRole("option", { name: /demo-private-subnets/ });
    await userEvent.selectOptions(select, "demo-private-subnets");
    await userEvent.click(screen.getByRole("button", { name: "Other target" }));
    await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(undefined));
    expect(select).toHaveValue("");
    expect(select).toHaveAccessibleDescription(/the earlier choice no longer fits; pick again/i);
    await userEvent.selectOptions(select, "other-account-subnets");
    expect(screen.queryByText(/no longer fits/)).toBeNull();
  });
});
