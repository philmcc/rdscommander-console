import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { resetCatalogueCache } from "@/features/tasks/catalogue";
import { TaskProvider } from "@/features/tasks/TaskContext";
import { resourceIdFromArn } from "@/lib/resourceId";
import { Launcher } from "./Launcher";

const bootstrap = {
  operator_name: "phil",
  console_version: "0.2.0",
  csrf_token: "t",
  environments: [
    {
      slug: "sandbox",
      display_name: "Sandbox",
      requires_approval: true,
      is_production: false,
      change_management: "direct",
      allow_direct_changes_to_foreign_owned: false,
      targets: [],
    },
  ],
};

/** The kinds the catalogue says run to the end without pausing for approval. */
const KINDS_THAT_RUN_WITHOUT_APPROVAL = new Set(["inventory_refresh", "drift_detect", "environment_import"]);

function definition(
  kind: string,
  title: string,
  group: string,
  appliesTo: string[],
  summary: string,
  properties: Record<string, unknown> = {},
) {
  return {
    kind,
    title,
    group,
    summary,
    applies_to: appliesTo,
    modes: ["terraform", "direct"],
    destructive: false,
    disruptive: false,
    needs_target: false,
    pauses_for_approval: !KINDS_THAT_RUN_WITHOUT_APPROVAL.has(kind),
    pauses_for_approval_when: kind === "drift_resolve" ? { accept: false, restore: true } : null,
    options_schema: { type: "object", properties },
  };
}

const catalogue = {
  definitions: [
    definition(
      "inventory_refresh",
      "Refresh inventory",
      "Discovery and adoption",
      ["environment"],
      "Read everything.",
    ),
    definition(
      "parameter_group_edit",
      "Edit parameters",
      "Parameter groups",
      ["parameter_group"],
      "Set values.",
    ),
  ],
};

function resourceRecord(kind: string, arn: string, identifier: string) {
  return {
    kind,
    arn,
    identifier,
    environment_slug: "sandbox",
    target_key: "t",
    ownership: "unmanaged",
    status: "available",
    facts: {},
    relationships: {},
    raw: {},
    snapshot_id: 1,
    terraform_address: null,
    parameter_apply_status: null,
  };
}

const parameterGroup = resourceRecord("parameter_group", "arn:pg", "demo-postgres16-tuned");

interface FetchOptions {
  definitions?: ReturnType<typeof definition>[];
  /** Resource records by kind; a kind that is absent answers 500. */
  resourcesByKind?: Record<string, ReturnType<typeof resourceRecord>[]>;
  catalogueStatus?: number;
}

/** The resource-list URLs the launcher asked for, since the last `stubFetch`. */
const resourceRequests: string[] = [];

/** Stubs `fetch`; operation creation is refused so the panel stays on its options and the request is kept. */
function stubFetch(options: FetchOptions = {}) {
  const posted: Record<string, unknown>[] = [];
  resourceRequests.length = 0;
  const resourcesByKind = options.resourcesByKind ?? { parameter_group: [parameterGroup] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
      if (url.endsWith("/api/v1/catalogue")) {
        if (options.catalogueStatus) return new Response("boom", { status: options.catalogueStatus });
        return Response.json({ definitions: options.definitions ?? catalogue.definitions });
      }
      if (url.endsWith("/resources"))
        return Response.json({ resources: Object.values(resourcesByKind).flat() });
      const listed = url.match(/\/resources\?(.+)$/);
      if (listed) {
        resourceRequests.push(url);
        const records = resourcesByKind[new URLSearchParams(listed[1]).get("kind") ?? ""];
        if (!records) return new Response("boom", { status: 500 });
        return Response.json({ resources: records });
      }
      if (url.endsWith("/api/v1/operations") && init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        return Response.json({ detail: "not now" }, { status: 409 });
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
  return posted;
}

async function openLauncher() {
  render(
    <MemoryRouter>
      <EnvironmentProvider>
        <TaskProvider>
          <Launcher />
        </TaskProvider>
      </EnvironmentProvider>
    </MemoryRouter>,
  );
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v1/bootstrap")));
  act(() => window.dispatchEvent(new CustomEvent("rdscommander:launcher")));
  return await screen.findByPlaceholderText("Start a task…");
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
  resetCatalogueCache();
});

describe("Launcher", () => {
  it("puts the task whose title matches first and leaves out tasks that only share scattered letters", async () => {
    stubFetch({
      definitions: [
        definition(
          "terraform_adoption",
          "Adopt resources",
          "Discovery and adoption",
          ["environment"],
          "Bring existing resources under Terraform with a reviewed import.",
        ),
        definition(
          "inventory_refresh",
          "Refresh inventory",
          "Discovery and adoption",
          ["environment"],
          "Read every instance, cluster, parameter group, subnet group and snapshot from AWS.",
        ),
        definition(
          "parameter_group_edit",
          "Edit parameters",
          "Parameter groups",
          ["parameter_group"],
          "Set or reset parameters with validation; reboot-needing parameters are flagged.",
        ),
        definition(
          "reboot_instance",
          "Reboot pending instances",
          "Parameter groups",
          ["instance"],
          "Reboot an instance with or without failover.",
        ),
      ],
    });
    const input = await openLauncher();
    await userEvent.type(input, "reboot");
    const options = screen.getAllByRole("option").map((option) => option.textContent ?? "");
    expect(options[0]).toContain("Reboot pending instances");
    expect(options.some((text) => text.includes("Edit parameters"))).toBe(true);
    expect(options.some((text) => text.includes("Adopt resources"))).toBe(false);
    expect(options.some((text) => text.includes("Refresh inventory"))).toBe(false);
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
  });

  it("ranks matches across groups, so the group whose task's title matches comes first and is selected", async () => {
    stubFetch({
      definitions: [
        definition(
          "terraform_adoption",
          "Adopt resources",
          "Discovery and adoption",
          ["environment"],
          "Bring existing resources under Terraform with a reviewed import.",
        ),
        definition(
          "inventory_refresh",
          "Refresh inventory",
          "Discovery and adoption",
          ["environment"],
          "Read every instance, cluster, parameter group, subnet group and snapshot from AWS.",
        ),
        definition(
          "environment_import",
          "Import environment",
          "Environments",
          ["environment"],
          "Add an AWS account and its regions as an environment, then read its inventory.",
        ),
      ],
    });
    const input = await openLauncher();
    await userEvent.type(input, "import");
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent ?? "")).toEqual([
      expect.stringContaining("Import environment"),
      expect.stringContaining("Adopt resources"),
    ]);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    const environmentsHeading = screen.getByText("Environments");
    const discoveryHeading = screen.getByText("Discovery and adoption");
    expect(
      environmentsHeading.compareDocumentPosition(discoveryHeading) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { name: "Import environment" })).toBeInTheDocument();
  });

  it("opens on the launcher event, filters, and asks for a resource when the entry needs one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/bootstrap")) return Response.json(bootstrap);
        if (url.endsWith("/api/v1/catalogue")) return Response.json(catalogue);
        if (url.includes("/resources?kind=parameter_group"))
          return Response.json({
            resources: [
              {
                kind: "parameter_group",
                arn: "arn:pg",
                identifier: "demo-postgres16-tuned",
                environment_slug: "sandbox",
                target_key: "t",
                ownership: "unmanaged",
                status: "available",
                facts: {},
                relationships: {},
                raw: {},
                snapshot_id: 1,
                terraform_address: null,
                parameter_apply_status: null,
              },
            ],
          });
        throw new Error(`unexpected ${url}`);
      }),
    );
    render(
      <MemoryRouter>
        <EnvironmentProvider>
          <TaskProvider>
            <Launcher />
          </TaskProvider>
        </EnvironmentProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/v1/bootstrap")));
    act(() => window.dispatchEvent(new CustomEvent("rdscommander:launcher")));
    const input = await screen.findByPlaceholderText("Start a task…");
    expect(screen.getByText("Refresh inventory")).toBeInTheDocument();
    await userEvent.type(input, "param");
    expect(screen.queryByText("Refresh inventory")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("Edit parameters"));
    expect(await screen.findByText("demo-postgres16-tuned")).toBeInTheDocument();
    await userEvent.click(screen.getByText("demo-postgres16-tuned"));
    expect(await screen.findByRole("dialog", { name: "Edit parameters" })).toBeInTheDocument();
  });

  it("lists every catalogue entry under its group", async () => {
    stubFetch();
    await openLauncher();
    expect(await screen.findByText("Discovery and adoption")).toBeInTheDocument();
    expect(screen.getByText("Parameter groups")).toBeInTheDocument();
    expect(screen.getByText("Read everything.")).toBeInTheDocument();
    expect(screen.getByText("Edit parameters")).toBeInTheDocument();
  });

  it("opens an environment-level entry for the selected environment", async () => {
    const posted = stubFetch();
    await openLauncher();
    await userEvent.click(await screen.findByText("Refresh inventory"));
    expect(await screen.findByRole("dialog", { name: "Refresh inventory" })).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Start a task…")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "inventory_refresh",
      environment_slug: "sandbox",
      resource_id: null,
    });
  });

  it("starts an instance or cluster create in the selected environment without asking for a resource", async () => {
    const posted = stubFetch({
      definitions: [
        definition(
          "instance_create",
          "Create instance",
          "Create and remove",
          ["environment"],
          "From the template.",
        ),
        definition(
          "cluster_create",
          "Create cluster",
          "Create and remove",
          ["environment"],
          "From the template.",
        ),
      ],
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Create cluster"));
    expect(await screen.findByRole("dialog", { name: "Create cluster" })).toBeInTheDocument();
    expect(resourceRequests).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "cluster_create",
      environment_slug: "sandbox",
      resource_id: null,
    });
  });

  it("opens an environment import without an environment", async () => {
    const posted = stubFetch({
      definitions: [
        definition("environment_import", "Import environment", "Environments", ["environment"], "Add one."),
      ],
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Import environment"));
    await userEvent.click(await screen.findByRole("button", { name: "Import environment" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ kind: "environment_import", environment_slug: null });
  });

  it("reads bootstrap again when an import started from the launcher is closed", async () => {
    stubFetch({
      definitions: [
        definition("environment_import", "Import environment", "Environments", ["environment"], "Add one."),
      ],
    });
    const bootstrapReads = () =>
      vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith("/api/v1/bootstrap")).length;
    await openLauncher();
    await userEvent.click(await screen.findByText("Import environment"));
    await screen.findByRole("dialog", { name: "Import environment" });
    expect(bootstrapReads()).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(bootstrapReads()).toBe(2));
  });

  it("clears the filter when it asks for a resource, and names the task being started", async () => {
    stubFetch();
    const input = await openLauncher();
    await userEvent.type(input, "param");
    await userEvent.click(await screen.findByText("Edit parameters"));
    const resourceInput = await screen.findByPlaceholderText("Choose a resource for Edit parameters…");
    expect(resourceInput).toHaveValue("");
    expect(await screen.findByText("demo-postgres16-tuned")).toBeInTheDocument();
  });

  it("opens the task for the chosen resource, by its id", async () => {
    const posted = stubFetch();
    await openLauncher();
    await userEvent.click(await screen.findByText("Edit parameters"));
    await userEvent.click(await screen.findByText("demo-postgres16-tuned"));
    expect(await screen.findByRole("dialog", { name: "Edit parameters" })).toBeInTheDocument();
    expect(screen.getByText("demo-postgres16-tuned")).toHaveClass("font-mono");
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "parameter_group_edit",
      environment_slug: "sandbox",
      resource_id: resourceIdFromArn("arn:pg"),
    });
  });

  it("lists resources of every kind the entry applies to, with the kind beside each, and the whole environment", async () => {
    const posted = stubFetch({
      definitions: [
        definition(
          "terraform_adoption",
          "Adopt resources",
          "Discovery and adoption",
          ["environment", "instance", "cluster", "parameter_group"],
          "Bring resources under Terraform.",
        ),
      ],
      resourcesByKind: {
        instance: [resourceRecord("instance", "arn:aws:rds:eu-west-2:0:db:demo-orders", "demo-orders")],
        cluster: [resourceRecord("cluster", "arn:aws:rds:eu-west-2:0:cluster:demo-aurora", "demo-aurora")],
        parameter_group: [parameterGroup],
      },
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Adopt resources"));
    const rows = await screen.findAllByRole("option");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Sandboxenvironment",
      "demo-ordersinstance",
      "demo-auroracluster",
      "demo-postgres16-tunedparameter group",
    ]);
    expect(screen.getByText("demo-orders")).toHaveClass("font-mono");
    await userEvent.click(screen.getByText("Sandbox"));
    expect(await screen.findByRole("dialog", { name: "Adopt resources" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "terraform_adoption",
      environment_slug: "sandbox",
      resource_id: null,
    });
  });

  it("asks for a resource when the task acts on one, and starts it on that resource's ARN", async () => {
    const instanceArn = "arn:aws:rds:eu-west-2:0:db:demo-orders";
    const posted = stubFetch({
      definitions: [
        definition(
          "drift_resolve",
          "Resolve drift",
          "Discovery and adoption",
          ["environment", "instance", "cluster", "parameter_group"],
          "Accept what AWS has, or restore it.",
          {
            resource_arn: {
              type: "string",
              ui: { widget: "hidden", help: "The drifted resource.", order: 1 },
            },
            choice: {
              type: "string",
              enum: ["accept", "restore"],
              ui: { widget: "choice", help: "", order: 2, labels: { accept: "Accept", restore: "Restore" } },
            },
          },
        ),
      ],
      resourcesByKind: {
        instance: [resourceRecord("instance", instanceArn, "demo-orders")],
        cluster: [],
        parameter_group: [parameterGroup],
      },
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Resolve drift"));
    const rows = await screen.findAllByRole("option");
    expect(rows.map((row) => row.textContent)).toEqual([
      "demo-ordersinstance",
      "demo-postgres16-tunedparameter group",
    ]);
    await userEvent.click(screen.getByText("demo-orders"));
    expect(await screen.findByRole("dialog", { name: "Resolve drift" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      kind: "drift_resolve",
      environment_slug: "sandbox",
      resource_id: resourceIdFromArn(instanceArn),
      options: { resource_arn: instanceArn },
    });
  });

  describe("adoption", () => {
    const instanceArn = "arn:aws:rds:eu-west-2:0:db:demo-orders";
    const adoption = definition(
      "terraform_adoption",
      "Adopt resources",
      "Discovery and adoption",
      ["environment", "instance"],
      "Bring resources under Terraform.",
      {
        target_key: { type: "string", ui: { widget: "target_picker", help: "", order: 1 } },
        resource_arns: {
          type: "array",
          items: { type: "string" },
          ui: {
            widget: "resource_picker",
            help: "",
            order: 2,
            kinds: ["instance"],
            ownership: ["unmanaged"],
          },
        },
      },
    );
    const instances = { instance: [resourceRecord("instance", instanceArn, "demo-orders")] };

    it("selects the chosen resource and its target", async () => {
      const posted = stubFetch({ definitions: [adoption], resourcesByKind: instances });
      await openLauncher();
      await userEvent.click(await screen.findByText("Adopt resources"));
      await userEvent.click(await screen.findByText("demo-orders"));
      expect(await screen.findByRole("dialog", { name: "Adopt resources" })).toBeInTheDocument();
      expect(await screen.findByRole("checkbox", { name: "demo-orders" })).toBeChecked();
      await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
      await waitFor(() => expect(posted).toHaveLength(1));
      expect(posted[0]).toMatchObject({
        kind: "terraform_adoption",
        resource_id: resourceIdFromArn(instanceArn),
        options: { target_key: "t", resource_arns: [instanceArn] },
      });
    });

    it("selects nothing when it is started on the whole environment", async () => {
      const posted = stubFetch({ definitions: [adoption], resourcesByKind: instances });
      await openLauncher();
      await userEvent.click(await screen.findByText("Adopt resources"));
      await userEvent.click(await screen.findByText("Sandbox"));
      expect(await screen.findByRole("dialog", { name: "Adopt resources" })).toBeInTheDocument();
      await userEvent.click(screen.getByRole("button", { name: "Continue to review" }));
      await waitFor(() => expect(posted).toHaveLength(1));
      expect(posted[0]).toMatchObject({ resource_id: null, options: {} });
    });
  });

  it("asks the backend only for the resources the task applies to, one request per kind", async () => {
    stubFetch({
      definitions: [
        definition(
          "reboot_instance",
          "Reboot pending instances",
          "Parameter groups",
          ["instance", "parameter_group"],
          "Reboot.",
        ),
      ],
      resourcesByKind: { instance: [], parameter_group: [parameterGroup] },
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Reboot pending instances"));
    expect(await screen.findByText("demo-postgres16-tuned")).toBeInTheDocument();
    expect([...resourceRequests].sort()).toEqual([
      "/api/v1/environments/sandbox/resources?kind=instance&applicable_to=reboot_instance",
      "/api/v1/environments/sandbox/resources?kind=parameter_group&applicable_to=reboot_instance",
    ]);
  });

  it("says when no resource of the kind has the task applying to it", async () => {
    stubFetch({ resourcesByKind: { parameter_group: [] } });
    await openLauncher();
    await userEvent.click(await screen.findByText("Edit parameters"));
    expect(
      await screen.findByText("No parameter groups this task applies to in Sandbox."),
    ).toBeInTheDocument();
  });

  it("names every kind the task applies to when none of them has a resource, and offers no environment", async () => {
    stubFetch({
      definitions: [
        definition(
          "drift_resolve",
          "Resolve drift",
          "Discovery and adoption",
          ["cluster", "environment", "instance", "parameter_group"],
          "Accept what AWS has, or restore it.",
          { resource_arn: { type: "string", ui: { widget: "hidden", help: "", order: 1 } } },
        ),
      ],
      resourcesByKind: { cluster: [], instance: [], parameter_group: [] },
    });
    await openLauncher();
    await userEvent.click(await screen.findByText("Resolve drift"));
    expect(
      await screen.findByText("No clusters, instances or parameter groups this task applies to in Sandbox."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("option")).not.toBeInTheDocument();
  });

  it("does not claim the task applies to nothing when the filter text matches nothing", async () => {
    stubFetch();
    await openLauncher();
    await userEvent.click(await screen.findByText("Edit parameters"));
    await screen.findByText("demo-postgres16-tuned");
    await userEvent.type(screen.getByPlaceholderText("Choose a resource for Edit parameters…"), "zzz");
    expect(await screen.findByText("Nothing matches.")).toBeInTheDocument();
    expect(screen.queryByText(/this task applies to/)).not.toBeInTheDocument();
  });

  it("says why the resources could not be listed, and goes back to the task list", async () => {
    stubFetch({ resourcesByKind: {} });
    await openLauncher();
    await userEvent.click(await screen.findByText("Edit parameters"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not list resources");
    await userEvent.click(screen.getByRole("button", { name: "All tasks" }));
    expect(await screen.findByPlaceholderText("Start a task…")).toBeInTheDocument();
    expect(screen.getByText("Refresh inventory")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says why the catalogue could not be loaded", async () => {
    stubFetch({ catalogueStatus: 500 });
    await openLauncher();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the task catalogue");
  });

  it("starts from the task list each time it opens", async () => {
    stubFetch();
    const input = await openLauncher();
    await userEvent.click(await screen.findByText("Edit parameters"));
    await screen.findByText("demo-postgres16-tuned");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(input).not.toBeInTheDocument());
    act(() => window.dispatchEvent(new CustomEvent("rdscommander:launcher")));
    expect(await screen.findByPlaceholderText("Start a task…")).toBeInTheDocument();
    expect(await screen.findByText("Refresh inventory")).toBeInTheDocument();
  });
});
