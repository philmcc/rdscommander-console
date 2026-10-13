import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SecurityGroupsField } from "./SecurityGroupsField";

afterEach(() => vi.unstubAllGlobals());

const PATTERN = "^sg-[0-9a-f]{8,17}$";

function instance(identifier: string, groupIds: string[], targetKey = "sandbox/0/eu-west-2") {
  return {
    kind: "instance",
    arn: `arn:aws:rds:eu-west-2:0:db:${identifier}`,
    identifier,
    target_key: targetKey,
    ownership: "unmanaged",
    raw: { VpcSecurityGroups: groupIds.map((id) => ({ VpcSecurityGroupId: id, Status: "active" })) },
  };
}

function stubResources(resources: object[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/environments/sandbox/resources") return Response.json({ resources });
      return new Response("not found", { status: 404 });
    }),
  );
}

function renderField(initial: string[] = []) {
  const onChange = vi.fn();
  function Harness() {
    const [value, setValue] = useState<unknown>(initial);
    return (
      <SecurityGroupsField
        name="vpc_security_group_ids"
        schema={{ type: "array" }}
        ui={{ widget: "security_groups", label: "Security groups", item_pattern: PATTERN }}
        value={value}
        onChange={(next) => {
          setValue(next);
          onChange(next);
        }}
        context={{ environmentSlug: "sandbox", targetKey: "sandbox/0/eu-west-2" }}
        formValue={{}}
      />
    );
  }
  render(<Harness />);
  return onChange;
}

describe("SecurityGroupsField", () => {
  it("adds a typed id and emits the list", async () => {
    stubResources();
    const onChange = renderField();
    await userEvent.type(screen.getByLabelText("Security group id"), "sg-0123456789abcdef0");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith(["sg-0123456789abcdef0"]);
    expect(screen.getByText("sg-0123456789abcdef0")).toBeInTheDocument();
    expect(screen.getByLabelText("Security group id")).toHaveValue("");
  });

  it("adds with Enter too", async () => {
    stubResources();
    const onChange = renderField();
    await userEvent.type(screen.getByLabelText("Security group id"), "sg-0123456789abcdef0{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(["sg-0123456789abcdef0"]);
  });

  it("refuses an id that is not a security group id, inline", async () => {
    stubResources();
    const onChange = renderField();
    await userEvent.type(screen.getByLabelText("Security group id"), "notagroup");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/must look like sg-/)).toBeInTheDocument();
    expect(screen.getByLabelText("Security group id")).toHaveAttribute("aria-invalid", "true");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("refuses an id already in the list", async () => {
    stubResources();
    const onChange = renderField(["sg-0123456789abcdef0"]);
    await userEvent.type(screen.getByLabelText("Security group id"), "sg-0123456789abcdef0");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(screen.getByText(/already in the list/)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("removes an id from the keyboard", async () => {
    stubResources();
    const onChange = renderField(["sg-0123456789abcdef0", "sg-00000000aaaaaaaa1"]);
    screen.getByRole("button", { name: "Remove sg-0123456789abcdef0" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onChange).toHaveBeenLastCalledWith(["sg-00000000aaaaaaaa1"]);
  });

  it("suggests the groups the target's databases use, and adds one in a click", async () => {
    stubResources([
      instance("demo-orders", ["sg-0aaaaaaaaaaaaaaa1"]),
      instance("demo-other", ["sg-0bbbbbbbbbbbbbbb2"], "production/1/eu-west-1"),
    ]);
    const onChange = renderField();
    const suggestion = await screen.findByRole("button", { name: /Add sg-0aaaaaaaaaaaaaaa1/ });
    expect(suggestion).toHaveTextContent("demo-orders");
    expect(screen.queryByRole("button", { name: /sg-0bbbbbbbbbbbbbbb2/ })).toBeNull();
    await userEvent.click(suggestion);
    expect(onChange).toHaveBeenLastCalledWith(["sg-0aaaaaaaaaaaaaaa1"]);
  });
});
