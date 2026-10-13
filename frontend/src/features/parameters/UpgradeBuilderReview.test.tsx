import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetBootstrapCache } from "@/api/client";
import type { UpgradePreview } from "@/api/types";
import { UpgradeBuilderReview } from "./UpgradeBuilderReview";

afterEach(() => {
  vi.unstubAllGlobals();
  resetBootstrapCache();
});

const instancePlan: UpgradePreview["plans"][number] = {
  scope: "instance",
  source_group_name: "demo-postgres16-tuned",
  source_family: "postgres16",
  target_family: "postgres17",
  new_group_name: "demo-postgres16-tuned-pg17",
  parameters_to_set: { work_mem: "16384" },
  already_built: false,
  classifications: [
    {
      name: "work_mem",
      value: "16384",
      outcome: "carried",
      new_name: null,
      note: "",
      old_default: "4096",
      new_default: "4096",
    },
    {
      name: "old_snapshot_threshold",
      value: "100",
      outcome: "removed",
      new_name: null,
      note: "old_snapshot_threshold does not exist in postgres17; dropped",
      old_default: "-1",
      new_default: null,
    },
    {
      name: "force_parallel_mode",
      value: "on",
      outcome: "renamed",
      new_name: "debug_parallel_query",
      note: "renamed",
      old_default: "off",
      new_default: "off",
    },
  ],
};

/** Answers bootstrap and the upgrade preview; `previewResponse` decides what the preview says. */
function stubApi(previewResponse: () => Response | Promise<Response>) {
  const previewRequests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/v1/bootstrap")) {
        return Response.json({
          csrf_token: "t",
          environments: [],
          operator_name: "",
          engine_version: "",
        });
      }
      if (url.endsWith("/api/v1/parameters/upgrade-preview")) {
        previewRequests.push(JSON.parse(String(init?.body)));
        return previewResponse();
      }
      throw new Error(`unexpected ${url}`);
    }),
  );
  return previewRequests;
}

function previewOf(...plans: UpgradePreview["plans"]): Response {
  return Response.json({ target_family_instance: "postgres17", target_family_cluster: null, plans });
}

describe("UpgradeBuilderReview", () => {
  it("shows classifications per scope and lets the operator edit carried values", async () => {
    const requests = stubApi(() => previewOf(instancePlan));
    const onChange = vi.fn();
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={onChange}
      />,
    );
    expect(await screen.findByText("demo-postgres16-tuned-pg17")).toBeInTheDocument();
    expect(requests).toEqual([{ resource_id: "YXJuOmk", target_engine_version: "17.2" }]);
    expect(screen.getByText("removed")).toBeInTheDocument();
    expect(screen.getByText("debug_parallel_query")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Value of work_mem"), "0");
    expect(onChange).toHaveBeenLastCalledWith({ instance: { work_mem: "163840" } });
  });

  it("makes every outcome but removed editable, files a rename under its new name, and lists what is dropped", async () => {
    stubApi(() => previewOf(instancePlan));
    const onChange = vi.fn();
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{ instance: { work_mem: "32768" } }}
        onChange={onChange}
      />,
    );
    await screen.findByText("demo-postgres16-tuned-pg17");
    // The operator's edit shows instead of the carried value.
    expect(screen.getByLabelText("Value of work_mem")).toHaveValue("32768");
    expect(screen.getByLabelText("Value of debug_parallel_query")).toHaveValue("on");
    expect(screen.queryByLabelText("Value of force_parallel_mode")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Value of old_snapshot_threshold")).not.toBeInTheDocument();
    const droppedRow = screen.getByText("old_snapshot_threshold").closest("tr");
    expect(droppedRow).not.toBeNull();
    expect(within(droppedRow as HTMLElement).getByText("dropped")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Value of debug_parallel_query"), "x");
    expect(onChange).toHaveBeenLastCalledWith({
      instance: { work_mem: "32768", debug_parallel_query: "onx" },
    });
  });

  it("starts a needs_review value empty, says why, and leaves the parameter out again when it is cleared", async () => {
    stubApi(() =>
      previewOf({
        ...instancePlan,
        classifications: [
          {
            name: "max_wal_size",
            value: "99999999",
            outcome: "needs_review",
            new_name: null,
            note: "99999999 is not valid in postgres17",
            old_default: "1024",
            new_default: "1024",
          },
        ],
      }),
    );
    const onChange = vi.fn();
    const { rerender } = render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={onChange}
      />,
    );
    const input = await screen.findByLabelText("Value of max_wal_size");
    expect(input).toHaveValue("");
    expect(screen.getByText("99999999 is not valid in postgres17")).toBeInTheDocument();
    await userEvent.type(input, "2");
    expect(onChange).toHaveBeenLastCalledWith({ instance: { max_wal_size: "2" } });
    rerender(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{ instance: { max_wal_size: "2" } }}
        onChange={onChange}
      />,
    );
    await userEvent.clear(screen.getByLabelText("Value of max_wal_size"));
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("says how a changed default compares and keeps the other scopes' edits when one is edited", async () => {
    const clusterPlan: UpgradePreview["plans"][number] = {
      ...instancePlan,
      scope: "cluster",
      source_group_name: "demo-aurora16-tuned",
      new_group_name: "demo-aurora16-tuned-pg17",
      classifications: [
        {
          name: "random_page_cost",
          value: "1.1",
          outcome: "default_changed",
          new_name: null,
          note: "",
          old_default: "4",
          new_default: "2",
        },
      ],
    };
    stubApi(() => previewOf(instancePlan, clusterPlan));
    const onChange = vi.fn();
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{ instance: { work_mem: "32768" } }}
        onChange={onChange}
      />,
    );
    expect(await screen.findByText("demo-aurora16-tuned-pg17")).toBeInTheDocument();
    expect(screen.getByText("(default 4 → 2)")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Value of random_page_cost"), "5");
    expect(onChange).toHaveBeenLastCalledWith({
      instance: { work_mem: "32768" },
      cluster: { random_page_cost: "1.15" },
    });
  });

  it("marks a group built earlier as kept and does not let its values be edited", async () => {
    stubApi(() => previewOf({ ...instancePlan, already_built: true }));
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={vi.fn()}
      />,
    );
    expect(await screen.findByText("already built — kept")).toBeInTheDocument();
    expect(screen.getByLabelText("Value of work_mem")).toBeDisabled();
  });

  it("says the new group starts from defaults when the current group has no overrides", async () => {
    stubApi(() => previewOf({ ...instancePlan, classifications: [], parameters_to_set: {} }));
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={vi.fn()}
      />,
    );
    const sentence = await screen.findByText(
      "No overrides on the current group; the new group starts from postgres17 defaults.",
    );
    // The sentence spans the table, so it wraps inside the 560px task panel instead of running off it.
    expect(sentence).toHaveClass("whitespace-normal");
  });

  it("asks for a version before it previews anything", () => {
    const requests = stubApi(() => previewOf(instancePlan));
    render(
      <UpgradeBuilderReview resourceId="YXJuOmk" targetEngineVersion="" overrides={{}} onChange={vi.fn()} />,
    );
    expect(screen.getByText("Choose a target version to preview the groups.")).toBeInTheDocument();
    expect(requests).toEqual([]);
  });

  it("shows why the preview failed", async () => {
    stubApi(() => Response.json({ detail: "17.2 is not an upgrade target of 16.4" }, { status: 422 }));
    render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={vi.fn()}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("17.2 is not an upgrade target of 16.4");
  });

  it("previews again for another version instead of showing the groups of the first", async () => {
    const requests = stubApi(() => previewOf(instancePlan));
    const { rerender } = render(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.2"
        overrides={{}}
        onChange={vi.fn()}
      />,
    );
    await screen.findByText("demo-postgres16-tuned-pg17");
    rerender(
      <UpgradeBuilderReview
        resourceId="YXJuOmk"
        targetEngineVersion="17.4"
        overrides={{}}
        onChange={vi.fn()}
      />,
    );
    expect(screen.queryByText("demo-postgres16-tuned-pg17")).not.toBeInTheDocument();
    expect(await screen.findByText("demo-postgres16-tuned-pg17")).toBeInTheDocument();
    expect(requests).toEqual([
      { resource_id: "YXJuOmk", target_engine_version: "17.2" },
      { resource_id: "YXJuOmk", target_engine_version: "17.4" },
    ]);
  });
});
