import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { InstanceClassField } from "./InstanceClassField";

afterEach(() => vi.unstubAllGlobals());

function stubFetch(classes: string[]) {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.startsWith("/api/v1/lifecycle/instance-classes")) return Response.json({ classes });
    if (path === "/api/v1/resources/abc")
      return Response.json({
        actions: [],
        recent_operations: [],
        record: { facts: { db_instance_class: "db.m6g.large" } },
      });
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

function renderField(initialValue = "") {
  const onChange = vi.fn();
  function Harness() {
    const [value, setValue] = useState(initialValue);
    return (
      <InstanceClassField
        name="instance_class"
        schema={{ type: "string" }}
        ui={{ widget: "instance_class", label: "Instance class" }}
        value={value}
        onChange={(next) => {
          setValue(String(next));
          onChange(next);
        }}
        context={{ environmentSlug: "sandbox", resourceId: "abc" }}
        formValue={{}}
      />
    );
  }
  render(<Harness />);
  return onChange;
}

describe("InstanceClassField", () => {
  it("offers the classes the endpoint lists and shows the current class", async () => {
    const fetchSpy = stubFetch(["db.m6g.large", "db.m6g.xlarge"]);
    renderField();
    await waitFor(() => expect(document.querySelectorAll("datalist option")).toHaveLength(2));
    expect([...document.querySelectorAll("datalist option")].map((o) => o.getAttribute("value"))).toEqual([
      "db.m6g.large",
      "db.m6g.xlarge",
    ]);
    expect(await screen.findByText("Current: db.m6g.large")).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/lifecycle/instance-classes?environment_slug=sandbox&resource_id=abc",
    );
  });

  it("emits a class chosen from the list", async () => {
    stubFetch(["db.m6g.large", "db.m6g.xlarge"]);
    const onChange = renderField();
    await waitFor(() => expect(document.querySelectorAll("datalist option")).toHaveLength(2));
    fireEvent.change(screen.getByLabelText("Instance class"), { target: { value: "db.m6g.xlarge" } });
    expect(onChange).toHaveBeenLastCalledWith("db.m6g.xlarge");
  });

  it("accepts a class typed in when the list is empty", async () => {
    stubFetch([]);
    const onChange = renderField();
    await userEvent.type(screen.getByLabelText("Instance class"), "db.t4g.micro");
    expect(onChange).toHaveBeenLastCalledWith("db.t4g.micro");
    expect(document.querySelectorAll("datalist option")).toHaveLength(0);
  });

  it("still works when the list cannot be fetched", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("boom", { status: 502 })),
    );
    const onChange = renderField();
    await userEvent.type(screen.getByLabelText("Instance class"), "x");
    expect(onChange).toHaveBeenLastCalledWith("x");
  });
});

describe("InstanceClassField on a create", () => {
  function renderCreateField(formValue: Record<string, unknown>) {
    render(
      <InstanceClassField
        name="instance_class"
        schema={{ type: "string" }}
        ui={{ widget: "instance_class", label: "Instance class", engine: "postgres" }}
        value=""
        onChange={vi.fn()}
        context={{ environmentSlug: "sandbox" }}
        formValue={formValue}
      />,
    );
  }

  it("asks for the classes of the engine and the version and target chosen in the form", async () => {
    const fetchSpy = stubFetch(["db.m6g.large"]);
    renderCreateField({ engine_version: "16.4", target_key: "sandbox/0/eu-west-2" });
    await waitFor(() => expect(document.querySelectorAll("datalist option")).toHaveLength(1));
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/v1/lifecycle/instance-classes?environment_slug=sandbox&target_key=sandbox%2F0%2Feu-west-2&engine=postgres&engine_version=16.4",
    );
  });

  it("says a version must be chosen before classes are listed", async () => {
    const fetchSpy = stubFetch(["db.m6g.large"]);
    renderCreateField({});
    expect(screen.getByText(/Choose an engine version to see the classes AWS offers/)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
